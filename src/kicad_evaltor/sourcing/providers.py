"""Distributor catalogues, looked up by exact part number.

Every provider answers ``lookup(number)`` with a :class:`PartRecord` for that
exact number, ``None`` when the catalogue has no such number, or raises
:class:`SourcingLookupError` (a :class:`LookupError`) when it could not give a
trustworthy answer: the network failed, the credentials were missing or
refused, or the reply was not the documented shape. An outage is never
reported as a missing part, and a fuzzy search hit is never taken as a match.

Only the standard library is used, so the ``sourcing`` extra adds nothing to
install. HTTP goes through a ``transport`` callable, which tests replace with
recorded replies.
"""

from __future__ import annotations

import http.client
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from abc import ABC, abstractmethod
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from kicad_evaltor.sourcing.record import PartRecord, clean_text, make_record


class SourcingLookupError(LookupError):
    """A catalogue could not give a trustworthy answer.

    ``platform`` names the catalogue, so a report can say which one failed.
    ``retryable`` is False for failures that asking again will not fix, such
    as missing or refused credentials.
    """

    def __init__(self, platform: str, message: str, *, retryable: bool = True) -> None:
        super().__init__(f"{platform}: {message}")
        self.platform = platform
        self.retryable = retryable


@dataclass(frozen=True)
class HttpResponse:
    status: int
    body: bytes


Transport = Callable[[urllib.request.Request, float], HttpResponse]
"""Send one request and return its status and body.

HTTP error statuses are returned, not raised; only a failure to get any reply
(DNS, refused connection, timeout) raises, as :class:`OSError`.
"""

USER_AGENT = "kicad-evaltor-sourcing (+https://github.com/attahiruj/kicad-evaltor)"


def urllib_transport(request: urllib.request.Request, timeout: float) -> HttpResponse:
    """The default transport: :func:`urllib.request.urlopen`."""
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return HttpResponse(response.status, response.read())
    except urllib.error.HTTPError as exc:
        body = exc.read() if exc.fp is not None else b""
        return HttpResponse(exc.code, body)


class SourcingProvider(ABC):
    """One distributor catalogue.

    ``platform`` is the short name a :class:`SourcingCheck` field maps to.
    """

    platform: str

    def normalize(self, number: str) -> str:
        """The number in the catalogue's canonical spelling.

        Raises :class:`ValueError` when ``number`` cannot be one of this
        catalogue's numbers at all, so a check can report a malformed number
        without querying anything.
        """
        cleaned = (number or "").strip()
        if not cleaned:
            raise ValueError(f"empty {self.platform} part number")
        return cleaned

    @abstractmethod
    def lookup(self, number: str) -> PartRecord | None:
        """The record for exactly ``number``, ``None`` if absent, or raise."""


def _decode_json(platform: str, body: bytes) -> Any:
    try:
        return json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SourcingLookupError(platform, f"reply is not JSON ({exc})") from exc


def _send(
    platform: str, transport: Transport, request: urllib.request.Request, timeout: float
) -> HttpResponse:
    try:
        return transport(request, timeout)
    except (OSError, http.client.HTTPException) as exc:
        raise SourcingLookupError(platform, f"request failed ({exc})") from exc


class _Retrying:
    """Retry an attempt that raised :class:`SourcingLookupError`, with backoff."""

    def __init__(self, attempts: int, backoff: float, sleep: Callable[[float], None]) -> None:
        if attempts < 1:
            raise ValueError("attempts must be at least 1")
        if backoff < 0:
            raise ValueError("backoff must not be negative")
        self.attempts = attempts
        self.backoff = backoff
        self.sleep = sleep

    def run(self, attempt: Callable[[], PartRecord | None]) -> PartRecord | None:
        for index in range(self.attempts):
            try:
                return attempt()
            except SourcingLookupError as exc:
                if not exc.retryable or index + 1 == self.attempts:
                    raise
                self.sleep(self.backoff * 2**index)
        raise AssertionError("unreachable")


# --------------------------------------------------------------------------
# LCSC / JLCPCB


class JlcpcbProvider(SourcingProvider):
    """LCSC numbers (``C25804``) through the public JLCPCB component search.

    The search is a keyword search, so it returns every listing whose text
    contains the number. Only the listing whose ``componentCode`` equals the
    number is accepted; the search is paged until that listing is found or
    every result has been seen. More than ``max_pages`` pages of results
    without a match is an incomplete answer, reported as a lookup error.

    No match means the number is absent from this catalogue, not proof that it
    never existed at LCSC.
    """

    platform = "lcsc"
    URL = "https://jlcpcb.com/api/overseas-pcb-order/v1/shoppingCart/smtGood/selectSmtComponentList"

    def __init__(
        self,
        *,
        timeout: float = 30.0,
        attempts: int = 3,
        backoff: float = 1.0,
        page_size: int = 100,
        max_pages: int = 10,
        transport: Transport | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        if page_size < 1 or max_pages < 1:
            raise ValueError("page_size and max_pages must be positive")
        self.timeout = timeout
        self.page_size = page_size
        self.max_pages = max_pages
        self.transport = transport or urllib_transport
        self._retry = _Retrying(attempts, backoff, sleep)

    def normalize(self, number: str) -> str:
        cleaned = (number or "").strip().upper()
        if not re.fullmatch(r"C[1-9]\d*", cleaned):
            raise ValueError(f"{number!r} is not an LCSC number (C followed by digits)")
        return cleaned

    def lookup(self, number: str) -> PartRecord | None:
        code = self.normalize(number)
        return self._retry.run(lambda: self._search(code))

    def _request(self, code: str, page: int) -> urllib.request.Request:
        body = {"keyword": code, "currentPage": page, "pageSize": self.page_size}
        return urllib.request.Request(
            self.URL,
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json", "User-Agent": USER_AGENT},
            method="POST",
        )

    def _search(self, code: str) -> PartRecord | None:
        page = 1
        while True:
            response = _send(self.platform, self.transport, self._request(code, page), self.timeout)
            if response.status != 200:
                raise SourcingLookupError(self.platform, f"HTTP {response.status}")
            payload = _decode_json(self.platform, response.body)
            try:
                if payload.get("code") != 200:
                    raise SourcingLookupError(
                        self.platform, f"search rejected: {payload.get('message')!r}"
                    )
                info = payload["data"]["componentPageInfo"]
                items = info["list"] or []
                total = int(info["total"])
                for item in items:
                    if str(item.get("componentCode", "")).upper() == code:
                        return self._record(item)
            except (KeyError, TypeError, ValueError, AttributeError) as exc:
                raise SourcingLookupError(self.platform, f"malformed reply ({exc!r})") from exc
            if page * self.page_size >= total:
                return None
            if not items or page >= self.max_pages:
                raise SourcingLookupError(
                    self.platform,
                    f"search for {code} was incomplete after {page} pages of {total} results",
                )
            page += 1

    def _record(self, item: Mapping[str, Any]) -> PartRecord:
        attributes = {
            attribute["attribute_name_en"]: attribute["attribute_value_name"]
            for attribute in item.get("attributes") or []
        }
        return make_record(
            number=str(item["componentCode"]),
            platform=self.platform,
            mpn=item.get("componentModelEn"),
            manufacturer=item.get("componentBrandEn"),
            category=item.get("componentTypeEn") or item.get("firstSortName"),
            package=item.get("componentSpecificationEn"),
            attributes=attributes,
            url=item.get("lcscGoodsUrl"),
            title=item.get("componentNameEn")
            or item.get("componentName")
            or item.get("componentModelEn"),
            description=item.get("describe"),
        )


# --------------------------------------------------------------------------
# Digi-Key


class DigiKeyProvider(SourcingProvider):
    """Digi-Key numbers (``296-1234-1-ND``) through Product Information API v4.

    Uses the ``productdetails`` endpoint, authenticated with OAuth client
    credentials (two-legged). The client ID and secret come from the
    ``DIGIKEY_CLIENT_ID`` and ``DIGIKEY_CLIENT_SECRET`` environment variables
    unless given; they are read at lookup time, and their absence is a lookup
    error, never a missing part.

    The endpoint also resolves a manufacturer part number, so the returned
    product is accepted only when ``number`` equals one of its Digi-Key
    product numbers or its manufacturer part number, ignoring case.

    Shapes follow Digi-Key's published OpenAPI document,
    https://developer.digikey.com/products/product-information-v4/productsearch/productdetails
    """

    platform = "digikey"
    BASE_URL = "https://api.digikey.com"
    SANDBOX_URL = "https://sandbox-api.digikey.com"

    def __init__(
        self,
        *,
        client_id: str | None = None,
        client_secret: str | None = None,
        client_id_env: str = "DIGIKEY_CLIENT_ID",
        client_secret_env: str = "DIGIKEY_CLIENT_SECRET",
        base_url: str = BASE_URL,
        site: str = "US",
        language: str = "en",
        currency: str = "USD",
        timeout: float = 30.0,
        attempts: int = 3,
        backoff: float = 1.0,
        transport: Transport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        self._client_id = client_id
        self._client_secret = client_secret
        self.client_id_env = client_id_env
        self.client_secret_env = client_secret_env
        self.base_url = base_url.rstrip("/")
        self.site = site
        self.language = language
        self.currency = currency
        self.timeout = timeout
        self.transport = transport or urllib_transport
        self._retry = _Retrying(attempts, backoff, sleep)
        self._clock = clock
        self._token: str | None = None
        self._token_expires = 0.0

    def _credentials(self) -> tuple[str, str]:
        client_id = self._client_id or os.environ.get(self.client_id_env, "")
        secret = self._client_secret or os.environ.get(self.client_secret_env, "")
        if not client_id or not secret:
            missing = [
                name
                for name, value in (
                    (self.client_id_env, client_id),
                    (self.client_secret_env, secret),
                )
                if not value
            ]
            raise SourcingLookupError(
                self.platform, f"no API credentials (set {' and '.join(missing)})", retryable=False
            )
        return client_id, secret

    def token_request(self) -> urllib.request.Request:
        client_id, secret = self._credentials()
        form = urllib.parse.urlencode(
            {"client_id": client_id, "client_secret": secret, "grant_type": "client_credentials"}
        ).encode()
        return urllib.request.Request(
            f"{self.base_url}/v1/oauth2/token",
            data=form,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "User-Agent": USER_AGENT,
            },
            method="POST",
        )

    def details_request(self, number: str, token: str) -> urllib.request.Request:
        client_id, _ = self._credentials()
        path = urllib.parse.quote(number, safe="")
        return urllib.request.Request(
            f"{self.base_url}/products/v4/search/{path}/productdetails",
            headers={
                "Authorization": f"Bearer {token}",
                "X-DIGIKEY-Client-Id": client_id,
                "X-DIGIKEY-Locale-Site": self.site,
                "X-DIGIKEY-Locale-Language": self.language,
                "X-DIGIKEY-Locale-Currency": self.currency,
                "Accept": "application/json",
                "User-Agent": USER_AGENT,
            },
            method="GET",
        )

    def _access_token(self) -> str:
        if self._token and self._clock() < self._token_expires:
            return self._token
        response = _send(self.platform, self.transport, self.token_request(), self.timeout)
        if response.status in (400, 401, 403):
            raise SourcingLookupError(
                self.platform,
                f"credentials rejected (HTTP {response.status}: {_problem(response.body)})",
                retryable=False,
            )
        if response.status != 200:
            raise SourcingLookupError(self.platform, f"token request HTTP {response.status}")
        payload = _decode_json(self.platform, response.body)
        try:
            token = str(payload["access_token"])
            lifetime = float(payload.get("expires_in", 0))
        except (KeyError, TypeError, ValueError, AttributeError) as exc:
            raise SourcingLookupError(self.platform, f"malformed token reply ({exc!r})") from exc
        if not token:
            raise SourcingLookupError(self.platform, "token reply has an empty access_token")
        self._token = token
        # Renew a little early so a token never expires mid-request.
        self._token_expires = self._clock() + max(0.0, lifetime - 30.0)
        return token

    def lookup(self, number: str) -> PartRecord | None:
        wanted = self.normalize(number)
        return self._retry.run(lambda: self._details(wanted))

    def _details(self, number: str) -> PartRecord | None:
        token = self._access_token()
        response = _send(
            self.platform, self.transport, self.details_request(number, token), self.timeout
        )
        if response.status == 401:
            # An expired token: forget it so the retry fetches a new one.
            self._token = None
            raise SourcingLookupError(self.platform, "token refused (HTTP 401)")
        if response.status == 403:
            raise SourcingLookupError(
                self.platform,
                f"access forbidden (HTTP 403: {_problem(response.body)})",
                retryable=False,
            )
        if response.status == 404:
            return None
        if response.status != 200:
            raise SourcingLookupError(
                self.platform, f"HTTP {response.status}: {_problem(response.body)}"
            )
        payload = _decode_json(self.platform, response.body)
        try:
            return self._record(number, payload["Product"])
        except (KeyError, TypeError, ValueError, AttributeError) as exc:
            raise SourcingLookupError(self.platform, f"malformed reply ({exc!r})") from exc

    def _record(self, number: str, product: Mapping[str, Any]) -> PartRecord | None:
        variations = [
            str(variation["DigiKeyProductNumber"])
            for variation in product.get("ProductVariations") or []
            if variation.get("DigiKeyProductNumber")
        ]
        mpn = str(product.get("ManufacturerProductNumber") or "")
        wanted = number.upper()
        matched = next((dk for dk in variations if dk.upper() == wanted), None)
        if matched is None and mpn.upper() != wanted:
            return None  # Digi-Key resolved some other product; not an exact match.
        attributes = {
            parameter["ParameterText"]: parameter.get("ValueText")
            for parameter in product.get("Parameters") or []
            if parameter.get("ParameterText")
        }
        description = product.get("Description") or {}
        return make_record(
            number=matched or mpn,
            platform=self.platform,
            mpn=mpn,
            manufacturer=(product.get("Manufacturer") or {}).get("Name"),
            category=" > ".join(_category_path(product.get("Category"))),
            package=attributes.get("Package / Case") or attributes.get("Supplier Device Package"),
            attributes=attributes,
            url=product.get("ProductUrl"),
            title=description.get("ProductDescription"),
            description=description.get("DetailedDescription"),
        )


def _category_path(node: Any) -> list[str]:
    """Category names from the top category down its child chain."""
    names: list[str] = []
    while isinstance(node, Mapping):
        if node.get("Name"):
            names.append(str(node["Name"]))
        children = node.get("ChildCategories") or []
        node = children[0] if len(children) == 1 else None
    return names


def _problem(body: bytes) -> str:
    """The human-readable part of an error reply, if it has one."""
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return clean_text(body[:200].decode("utf-8", "replace")) or "no detail"
    if isinstance(payload, Mapping):
        for key in ("detail", "title", "ErrorMessage", "message", "Message"):
            if payload.get(key):
                return clean_text(payload[key])
    return "no detail"


# --------------------------------------------------------------------------
# Mouser


class MouserProvider(SourcingProvider):
    """Mouser numbers (``595-BQ24074RGTR``) through the Search API.

    Uses the part-number search with ``partSearchOptions`` set to ``Exact``,
    and still accepts only the listing whose ``MouserPartNumber`` equals the
    number, ignoring case. The API key comes from ``MOUSER_API_KEY`` unless
    given; it is read at lookup time, and its absence is a lookup error.
    Mouser reports a refused key in the reply's ``Errors`` list, which is
    also a lookup error.

    Shapes follow Mouser's published OpenAPI document,
    https://api.mouser.com/api/docs/V1 (``SearchByPartRequestRoot``,
    ``SearchResponseRoot``).
    """

    platform = "mouser"
    URL = "https://api.mouser.com/api/v1/search/partnumber"

    def __init__(
        self,
        *,
        api_key: str | None = None,
        api_key_env: str = "MOUSER_API_KEY",
        timeout: float = 30.0,
        attempts: int = 3,
        backoff: float = 1.0,
        transport: Transport | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        self._api_key = api_key
        self.api_key_env = api_key_env
        self.timeout = timeout
        self.transport = transport or urllib_transport
        self._retry = _Retrying(attempts, backoff, sleep)

    def normalize(self, number: str) -> str:
        cleaned = super().normalize(number)
        # The API takes up to ten numbers separated by "|"; one field is one part.
        if "|" in cleaned or not 3 <= len(cleaned) <= 40:
            raise ValueError(f"{number!r} is not a single Mouser part number")
        return cleaned

    def _key(self) -> str:
        key = self._api_key or os.environ.get(self.api_key_env, "")
        if not key:
            raise SourcingLookupError(
                self.platform, f"no API key (set {self.api_key_env})", retryable=False
            )
        return key

    def search_request(self, number: str) -> urllib.request.Request:
        query = urllib.parse.urlencode({"apiKey": self._key()})
        body = {"SearchByPartRequest": {"mouserPartNumber": number, "partSearchOptions": "Exact"}}
        return urllib.request.Request(
            f"{self.URL}?{query}",
            data=json.dumps(body).encode(),
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": USER_AGENT,
            },
            method="POST",
        )

    def lookup(self, number: str) -> PartRecord | None:
        wanted = self.normalize(number)
        return self._retry.run(lambda: self._search(wanted))

    def _search(self, number: str) -> PartRecord | None:
        response = _send(self.platform, self.transport, self.search_request(number), self.timeout)
        if response.status in (401, 403):
            raise SourcingLookupError(
                self.platform, f"API key rejected (HTTP {response.status})", retryable=False
            )
        if response.status != 200:
            raise SourcingLookupError(
                self.platform, f"HTTP {response.status}: {_problem(response.body)}"
            )
        payload = _decode_json(self.platform, response.body)
        try:
            errors = payload.get("Errors") or []
            if errors:
                messages = "; ".join(
                    clean_text(error.get("Message") or error.get("Code") or error)
                    for error in errors
                )
                # Mouser answers a bad key with HTTP 200 and an Errors entry;
                # retrying will not fix it.
                raise SourcingLookupError(
                    self.platform, f"search rejected: {messages}", retryable=False
                )
            results = payload["SearchResults"]
            parts = (results or {}).get("Parts") or []
            wanted = number.upper()
            for part in parts:
                if str(part.get("MouserPartNumber") or "").upper() == wanted:
                    return self._record(part)
        except (KeyError, TypeError, ValueError, AttributeError) as exc:
            raise SourcingLookupError(self.platform, f"malformed reply ({exc!r})") from exc
        return None

    def _record(self, part: Mapping[str, Any]) -> PartRecord:
        attributes: dict[str, str] = {}
        for attribute in part.get("ProductAttributes") or []:
            name = attribute.get("AttributeName")
            if not name:
                continue
            value = str(attribute.get("AttributeValue") or "")
            # A name may repeat (several "Packaging" rows); keep every value.
            attributes[name] = f"{attributes[name]}; {value}" if name in attributes else value
        return make_record(
            number=str(part["MouserPartNumber"]),
            platform=self.platform,
            mpn=part.get("ManufacturerPartNumber"),
            manufacturer=part.get("Manufacturer"),
            category=part.get("Category"),
            package=attributes.get("Package / Case", ""),
            attributes=attributes,
            url=part.get("ProductDetailUrl"),
            title=part.get("Description"),
            description=part.get("Description"),
        )


def default_provider(platform: str) -> SourcingProvider:
    """A provider for a built-in platform name: lcsc, digikey or mouser."""
    factories: dict[str, Callable[[], SourcingProvider]] = {
        "lcsc": JlcpcbProvider,
        "jlcpcb": JlcpcbProvider,
        "digikey": DigiKeyProvider,
        "mouser": MouserProvider,
    }
    try:
        return factories[platform.lower()]()
    except KeyError:
        raise ValueError(
            f"No built-in provider for platform {platform!r}; pass one in providers="
        ) from None
