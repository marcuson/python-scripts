"""Suggest Italian names through Google Cloud Translation Basic v2."""

import http.client
import json
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast

from mealietidy.export import json_bytes, sha256, write_new

HOST: str = "translation.googleapis.com"
ENDPOINT: str = "/language/translate/v2"
BATCH_SIZE: int = 19
MAX_REQUEST_CHARS: int = 5_000
MAX_RUN_CHARS: int = 16_000


class GTranslateProvider:
    name: str = "gtranslate"
    metadata: Mapping[str, str | None] = {"model": "nmt"}

    def __init__(self, root: Path, api_key: str, timeout: float = 20.0) -> None:
        self.root: Path = root
        self.api_key: str = api_key
        self.timeout: float = timeout

    def suggest(
        self, foods: list[dict[str, Any]], context: dict[str, Any]
    ) -> tuple[list[dict[str, Any]], list[list[str]]]:
        total: int = len(foods)
        sent_chars: int = 0
        suggestions: list[dict[str, Any]] = []
        failed_batches: list[list[str]] = []
        print(f"gtranslate: 0/{total} foods processed", file=sys.stderr, flush=True)
        for start in range(0, total, BATCH_SIZE):
            batch: list[dict[str, Any]] = foods[start : start + BATCH_SIZE]
            names: list[str] = [food["name"] for food in batch]
            characters: int = sum(len(name) for name in names)
            request: dict[str, object] = {
                "q": names,
                "target": "it",
                "format": "text",
                "model": "nmt",
            }
            cache: Path = (
                self.root
                / "suggestions-cache"
                / f"gtranslate-{sha256(json_bytes(request))}.json"
            )
            try:
                if cache.is_file():
                    raw: object = json.loads(cache.read_bytes())
                else:
                    if characters > MAX_REQUEST_CHARS:
                        raise ValueError(
                            "batch exceeds the local 5,000-character request limit"
                        )
                    if sent_chars + characters > MAX_RUN_CHARS:
                        raise ValueError("16,000-character run limit reached")
                    sent_chars += characters
                    raw = self._request(request)
                batch_suggestions: list[dict[str, Any]] = self._validate(raw, batch)
                if not cache.is_file():
                    cache.parent.mkdir(mode=0o700, exist_ok=True)
                    write_new(cache, json_bytes(raw))
                suggestions.extend(batch_suggestions)
            except (OSError, RuntimeError, ValueError, TypeError) as error:
                print(
                    f"mealietidy: warning: gtranslate suggestions unavailable: {error}",
                    file=sys.stderr,
                )
                failed_batches.append([food["id"] for food in batch])
            print(
                f"gtranslate: {min(start + BATCH_SIZE, total)}/{total} foods processed",
                file=sys.stderr,
                flush=True,
            )
        return suggestions, failed_batches

    def _request(self, request: dict[str, object]) -> object:
        connection: http.client.HTTPSConnection = http.client.HTTPSConnection(
            HOST, timeout=self.timeout
        )
        try:
            connection.request(
                "POST",
                ENDPOINT,
                body=json_bytes(request),
                headers={
                    "Content-Type": "application/json; charset=utf-8",
                    "X-goog-api-key": self.api_key,
                },
            )
            response: http.client.HTTPResponse = connection.getresponse()
            payload: bytes = response.read()
            if response.status != 200:
                raise RuntimeError(
                    f"Google Translation API returned HTTP {response.status}"
                )
        except (OSError, http.client.HTTPException):
            raise RuntimeError("Google Translation API request failed") from None
        finally:
            connection.close()
        try:
            return json.loads(payload)
        except json.JSONDecodeError:
            raise ValueError("Google Translation API returned invalid JSON") from None

    def _validate(
        self, value: object, batch: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        if not isinstance(value, dict):
            raise TypeError("Google Translation response must be an object")
        outer: dict[str, object] = cast(dict[str, object], value)
        data: object = outer.get("data")
        if not isinstance(data, dict):
            raise TypeError("Google Translation response has no data object")
        translations: object = cast(dict[str, object], data).get("translations")
        if not isinstance(translations, list):
            raise TypeError("Google Translation returned an invalid result list")
        results: list[object] = cast(list[object], translations)
        if len(results) != len(batch):
            raise ValueError("Google Translation returned an unexpected result count")
        suggestions: list[dict[str, Any]] = []
        for food, item in zip(batch, results, strict=True):
            if not isinstance(item, dict):
                raise TypeError("Google Translation returned an invalid result")
            translation: dict[str, object] = cast(dict[str, object], item)
            translated_value: object = translation.get("translatedText")
            source: object = translation.get("detectedSourceLanguage")
            if (
                not isinstance(translated_value, str)
                or not isinstance(source, str)
                or not 1 <= len(source) <= 16
                or not source.isascii()
                or not source.replace("-", "").isalnum()
            ):
                raise TypeError("Google Translation returned an invalid translation")
            proposed: str = translated_value.strip()
            if (
                source == "it"
                or not proposed
                or len(proposed) > 255
                or any(ord(char) < 32 for char in proposed)
                or proposed.casefold() == food["name"].strip().casefold()
            ):
                continue
            suggestions.append(
                {
                    "food_id": food["id"],
                    "action": "rename",
                    "value": proposed,
                    "language": "it",
                    "rationale": f"Google Cloud Translation from {source}; verify culinary meaning",
                    "uncertainty": "high",
                    "related_candidate_ids": [],
                }
            )
        return suggestions
