"""Клиент Overpass API: зеркала, повторы, backoff с jitter, /api/status, кэш (раздел 8.1 ТЗ)."""
from __future__ import annotations

import json
import logging
import random
import re
import time
from typing import Any

import requests

from ..cache import ResponseCache, query_hash
from ..errors import NetworkError, ParseError, QueryTooLarge
from .base import Provider

log = logging.getLogger("osmdwnl")

RETRY_STATUS = {429, 502, 503, 504}
BUSY_RE = re.compile(r"(too busy|Dispatcher_Client|rate_limited|Too Many Requests)", re.I)
TOO_LARGE_RE = re.compile(r"(timed out|out of memory|run out of memory|maxsize|Query run out)", re.I)


class OverpassProvider(Provider):
    name = "overpass"

    def __init__(self, cfg, cache: ResponseCache, recipe_tag: str = "", session=None):
        self.cfg = cfg
        self.cache = cache
        self.recipe_tag = recipe_tag
        self.session = session or requests.Session()
        self.session.headers.update({"User-Agent": cfg.user_agent, "Accept": "application/json"})
        self._ep = 0
        self._cool: dict[str, float] = {}   # зеркало -> время, до которого его не используем
        self._strikes: dict[str, int] = {}  # подряд неудачных «медленных» ответов зеркала
        self._last_request = 0.0
        self.executed: list[dict[str, Any]] = []   # журнал физических запросов
        self._last_ok = [0.0]                # время последнего успешного ответа (общее для клонов)

    def clone(self, start_index: int) -> "OverpassProvider":
        """Копия для параллельной работы: своё текущее зеркало, общие паузы зеркал и журнал."""
        c = type(self)(self.cfg, self.cache, self.recipe_tag)
        c._ep = start_index
        c._cool, c._strikes, c.executed, c._last_ok = self._cool, self._strikes, self.executed, self._last_ok
        return c

    @property
    def endpoint(self) -> str:
        return self.cfg.endpoints[self._ep % len(self.cfg.endpoints)]

    def _alive(self, ep: str) -> bool:
        return self._cool.get(ep, 0.0) <= time.time()

    def _switch(self) -> None:
        """Следующее зеркало, пропуская временно отключённые (недоступные/зависающие)."""
        n = len(self.cfg.endpoints)
        for _ in range(n):
            self._ep += 1
            if self._alive(self.endpoint):
                return
        # все на паузе — берём то, у которого пауза кончается раньше
        self._ep = min(range(n), key=lambda i: self._cool.get(self.cfg.endpoints[i], 0.0))

    def _slow_penalty(self, ep: str) -> float:
        """Пауза для медленного зеркала растёт с каждым неудачным разом подряд: 2, 4, 8… мин (≤ 30)."""
        k = self._strikes.get(ep, 0) + 1
        self._strikes[ep] = k
        return min(1800.0, self.cfg.slow_endpoint_cooldown_seconds * 2 ** (k - 1))

    def _penalize(self, ep: str, exc: Exception) -> None:
        """Зеркало без соединения — пауза надолго; медленный ответ/обрыв — коротко.

        requests оборачивает тайм-аут чтения тела ответа в ConnectionError («Read timed out.»),
        поэтому тип определяется и по тексту исключения."""
        if len(self.cfg.endpoints) < 2:
            return
        text = str(exc)
        connect_fail = isinstance(exc, requests.ConnectTimeout) or (
            isinstance(exc, requests.ConnectionError)
            and re.search(r"NewConnectionError|NameResolution|Failed to establish|refused|"
                          r"getaddrinfo|ConnectTimeout|connect timeout", text, re.I)
            and "Read timed out" not in text)
        if connect_fail:
            sec = self.cfg.endpoint_cooldown_seconds
        elif isinstance(exc, (requests.ReadTimeout, requests.ConnectionError)):
            sec = self._slow_penalty(ep)
        else:
            return
        self._cool[ep] = time.time() + sec
        log.warning("зеркало %s временно не используется (%.0f мин): %s", ep, sec / 60,
                    "нет соединения" if connect_fail else "ответ не дождались")

    def header(self) -> str:
        return f"[out:json][timeout:{int(self.cfg.timeout_seconds)}][maxsize:{int(self.cfg.maxsize_bytes)}];"

    def light_header(self) -> str:
        return light_header(self.cfg)

    # ---------------------------------------------------------------
    def execute(self, query: str, purpose: str = "", split_after_busy: int = 0) -> dict[str, Any]:
        """split_after_busy=N: если этому запросу N раз подряд отказали «перегружен», а другие запросы
        за последние 10 мин проходили, — QueryTooLarge (тайл будет разделён на 4)."""
        for ep in [self.endpoint] + [e for e in self.cfg.endpoints if e != self.endpoint]:
            cached = self.cache.get(query_hash(query, ep, self.recipe_tag))
            if cached is not None:
                log.debug("кэш: %s (%s)", purpose, ep)
                self.executed.append({"purpose": purpose, "endpoint": ep, "cached": True,
                                      "hash": query_hash(query, ep, self.recipe_tag)})
                return cached

        errors_on_endpoint = 0
        last_exc: Exception | None = None
        attempt, busy_left = 0, self.cfg.busy_retries
        busy_hits, t_call = 0, time.time()

        def stuck() -> bool:
            return (split_after_busy > 0 and busy_hits >= split_after_busy
                    and self._last_ok[0] > t_call - 600)

        while attempt < self.cfg.retries:
            attempt += 1
            if not self._alive(self.endpoint):
                self._switch()
            ep = self.endpoint
            key = query_hash(query, ep, self.recipe_tag)
            self._wait_slot(ep)
            t0 = time.time()
            try:
                resp = self.session.post(ep, data={"data": query},
                                         timeout=(self.cfg.connect_timeout_seconds,
                                                  self.cfg.timeout_seconds + 60))
                self._last_request = time.time()
            except requests.RequestException as exc:
                last_exc = exc
                self._penalize(ep, exc)
                log.warning("%s: сетевая ошибка (попытка %d/%d, %s): %s", purpose, attempt,
                            self.cfg.retries, ep, exc)
                errors_on_endpoint = self._after_error(errors_on_endpoint, attempt)
                continue
            dt = time.time() - t0
            size = len(resp.content)
            log.info("%s: HTTP %d, %.1f с, %.1f КБ, попытка %d, %s", purpose, resp.status_code, dt,
                     size / 1024, attempt, ep)
            if resp.status_code == 200:
                self._strikes.pop(ep, None)
                self._last_ok[0] = time.time()
                data = self._decode(resp, purpose)
                remark = str(data.get("remark") or "")
                if remark and "runtime error" in remark.lower():
                    if BUSY_RE.search(remark) and busy_left > 0:
                        busy_hits += 1
                        if stuck():
                            raise QueryTooLarge("сервер раз за разом отказывает этому запросу",
                                                cause=f"{busy_hits} отказов «перегружен» подряд, "
                                                      "другие запросы проходят", action="Тайл будет разделён")
                        busy_left -= 1
                        attempt -= 1
                        n = self.cfg.busy_retries - busy_left
                        if len(self.cfg.endpoints) > 1:
                            self._switch()
                        delay = min(self.cfg.backoff_max_seconds, 2 * self.cfg.backoff_base_seconds * n) * random.uniform(0.8, 1.2)
                        log.info("%s: сервер Overpass перегружен — пауза %.0f с", purpose, delay)
                        time.sleep(delay)
                        last_exc = NetworkError("Overpass перегружен", cause=remark)
                        continue
                    if TOO_LARGE_RE.search(remark):
                        self.cache.record_failure(key)
                        raise QueryTooLarge("Overpass: запрос слишком тяжёлый", cause=remark,
                                            action="Тайл будет разделён")
                    raise NetworkError("Overpass вернул ошибку выполнения", cause=remark)
                if remark:
                    log.warning("%s: remark Overpass: %s", purpose, remark)
                self.cache.put(key, data)
                self.executed.append({"purpose": purpose, "endpoint": ep, "cached": False,
                                      "hash": key, "seconds": round(dt, 2), "bytes": size})
                return data
            if resp.status_code == 400:
                raise NetworkError("Overpass отклонил запрос (HTTP 400, синтаксис QL)",
                                   cause=_html_error(resp.text),
                                   action="Это ошибка генерации запроса/рецепта; запустите --dry-run и проверьте рецепт")
            if resp.status_code in RETRY_STATUS:
                # 429, сообщение диспетчера Overpass или короткая страница 50x от прокси —
                # это перегрузка сервера, а не тяжёлый запрос
                busy = (resp.status_code == 429 or BUSY_RE.search(resp.text) is not None
                        or (resp.status_code in (502, 503, 504) and size < 400))
                if busy and busy_left > 0:
                    busy_hits += 1
                    if stuck():
                        raise QueryTooLarge("сервер раз за разом отказывает этому запросу",
                                            cause=f"{busy_hits} отказов «перегружен» подряд, "
                                                  "другие запросы проходят", action="Тайл будет разделён")
                    # сервер перегружен — запрос не «тяжёлый», делить тайл бессмысленно;
                    # такие ответы не расходуют обычные попытки, ждём дольше и пробуем зеркало
                    busy_left -= 1
                    attempt -= 1
                    n = self.cfg.busy_retries - busy_left
                    if dt > 60:      # зеркало отвечает «перегружен» только через минуты — пауза для него
                        sec = self._slow_penalty(ep)
                        self._cool[ep] = time.time() + sec
                        log.warning("зеркало %s отвечает «перегружен» через %.0f с — не используется %.0f мин",
                                    ep, dt, sec / 60)
                    # быстрый отказ «перегружен» — ждём и повторяем там же; зеркало — с 3-го раза
                    switched = False
                    if len(self.cfg.endpoints) > 1 and (n % 3 == 0 or not self._alive(ep)):
                        self._switch()
                        switched = self.endpoint != ep
                    if switched:     # на другом сервере ждать долго незачем
                        delay = min(5.0, self.cfg.backoff_base_seconds)
                    else:
                        delay = min(60.0, self.cfg.backoff_max_seconds,
                                    2 * self.cfg.backoff_base_seconds * n) * random.uniform(0.8, 1.2)
                    log.info("%s: сервер Overpass перегружен (HTTP %d) — пауза %.0f с, затем %s",
                             purpose, resp.status_code, delay, self.endpoint)
                    time.sleep(delay)
                    last_exc = NetworkError(f"HTTP {resp.status_code} (сервер перегружен)")
                    continue
                if resp.status_code == 504 and "timeout" in resp.text.lower() and attempt >= 2 and not busy:
                    self.cache.record_failure(key)
                    raise QueryTooLarge("Overpass: gateway timeout", cause=_html_error(resp.text))
                last_exc = NetworkError(f"HTTP {resp.status_code}", cause=_html_error(resp.text))
                errors_on_endpoint = self._after_error(errors_on_endpoint, attempt)
                continue
            raise NetworkError(f"Overpass: неожиданный ответ HTTP {resp.status_code}",
                               cause=_html_error(resp.text))
        raise NetworkError("Overpass недоступен после всех попыток", cause=str(last_exc),
                           action="Повторите позже (готовые тайлы возьмутся из кэша) или смените зеркало в config.yml")

    # ---------------------------------------------------------------
    def _decode(self, resp, purpose) -> dict[str, Any]:
        try:
            data = resp.json()
        except ValueError as exc:
            txt = resp.text[:500]
            if TOO_LARGE_RE.search(txt):
                raise QueryTooLarge("Overpass: запрос слишком тяжёлый", cause=_html_error(txt)) from exc
            raise ParseError(f"{purpose}: ответ Overpass не является JSON", cause=txt) from exc
        if not isinstance(data, dict) or "elements" not in data:
            raise ParseError(f"{purpose}: в ответе нет 'elements'")
        return data

    def _after_error(self, errors_on_endpoint: int, attempt: int) -> int:
        errors_on_endpoint += 1
        if (errors_on_endpoint >= self.cfg.switch_after_errors or not self._alive(self.endpoint)) \
                and len(self.cfg.endpoints) > 1:
            self._switch()
            log.warning("переключение на зеркало %s", self.endpoint)
            errors_on_endpoint = 0
        delay = min(self.cfg.backoff_max_seconds, self.cfg.backoff_base_seconds * 2 ** (attempt - 1))
        delay *= random.uniform(0.7, 1.3)
        log.info("пауза %.0f с перед повтором", delay)
        time.sleep(delay)
        return errors_on_endpoint

    def _wait_slot(self, ep: str) -> None:
        gap = time.time() - self._last_request
        if gap < self.cfg.min_pause_seconds:
            time.sleep(self.cfg.min_pause_seconds - gap)
        if not self.cfg.check_status:
            return
        url = ep.rsplit("/", 1)[0] + "/status"
        try:
            r = self.session.get(url, timeout=(10, 20))
            if r.status_code != 200:
                return
            text = r.text
        except requests.RequestException:
            return
        if re.search(r"(\d+) slots? available now", text):
            return
        waits = [int(x) for x in re.findall(r"in (-?\d+) seconds", text)]
        if waits:
            w = max(0, min(min(waits), self.cfg.status_max_wait_seconds))
            if w:
                log.info("Overpass: свободный слот через %d с — ожидание", w)
                time.sleep(w + 1)


def light_header(cfg) -> str:
    """Заголовок «лёгкого» запроса (тайлы, границы, is_in): перегруженный Overpass охотнее
    принимает запросы, заявляющие меньше памяти и времени."""
    return f"[out:json][timeout:{int(cfg.tile_timeout_seconds)}][maxsize:{int(cfg.tile_maxsize_bytes)}];"


def _html_error(text: str) -> str:
    msgs = re.findall(r"<strong[^>]*>Error</strong>:?(.*?)</p>", text, flags=re.S | re.I)
    if msgs:
        return " | ".join(re.sub(r"<[^>]+>", "", m).strip() for m in msgs)[:800]
    try:
        return json.dumps(json.loads(text).get("remark"))[:800]
    except ValueError:
        return re.sub(r"<[^>]+>", " ", text)[:400].strip()
