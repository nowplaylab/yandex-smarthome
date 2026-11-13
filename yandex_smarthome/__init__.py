"""Умный дом Яндекса из Python: вход по коду (без пароля) и управление устройствами, прежде всего светом.

    login = DeviceLogin(client_id, client_secret)
    code = await login.start(http)               # покажите человеку code.user_code и code.url
    token = await login.wait(http, code)         # ждём, пока он введёт код на странице Яндекса
    home = SmartHome(token.access)
    for light in await home.lights(http):
        await home.actions(http, {light.id: light_actions(light, hsv=(30, 0.9), brightness=80)})

Нужно своё приложение на https://oauth.yandex.ru/client/new с правами iot:view и iot:control.
Документация API: https://yandex.ru/dev/dialogs/smart-home/doc/ru/concepts/platform-protocol
"""
import asyncio
import colorsys
import time
from dataclasses import dataclass, field

import aiohttp

__version__ = '0.2.0'
__all__ = ['YandexError', 'AuthError', 'LoginError', 'LoginCode', 'Token', 'DeviceLogin',
           'Light', 'SmartHome', 'light_actions']

OAUTH = 'https://oauth.yandex.ru'
IOT = 'https://api.iot.yandex.net'


class YandexError(Exception):
    """Яндекс не ответил или ответил ошибкой."""


class AuthError(YandexError):
    """Вход больше не действует — нужно обновить токен или войти заново."""


class LoginError(YandexError):
    """Вход по коду не удался. reason: expired (код устарел), denied (не разрешили), other."""

    def __init__(self, reason, message=''):
        super().__init__(message or reason)
        self.reason = reason


@dataclass
class LoginCode:
    device_code: str
    user_code: str
    url: str
    interval: int
    expires_at: float


@dataclass
class Token:
    access: str
    refresh: str = ''
    expires_at: float = 0.0


class DeviceLogin:
    """Вход по коду подтверждения: https://yandex.ru/dev/id/doc/ru/codes/screen-code-oauth"""

    def __init__(self, client_id, client_secret='', oauth=OAUTH):
        self.client_id, self.client_secret, self.oauth = client_id, client_secret, oauth

    def _auth(self):
        d = {'client_id': self.client_id}
        if self.client_secret:
            d['client_secret'] = self.client_secret
        return d

    async def start(self, http, device_id=None, device_name=None):
        body = {'client_id': self.client_id}
        if device_id:
            body['device_id'] = device_id
            if device_name:
                body['device_name'] = device_name[:100]
        try:
            async with http.post(self.oauth + '/device/code', data=body) as r:
                d = await r.json(content_type=None)
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as e:
            raise YandexError(f'нет связи с Яндексом: {e}') from None
        if 'device_code' not in d:
            raise YandexError(str(d.get('error_description') or d.get('error') or d))
        return LoginCode(d['device_code'], d['user_code'], d.get('verification_url') or 'https://oauth.yandex.ru/device',
                         int(d.get('interval') or 5), time.time() + int(d.get('expires_in') or 300))

    def _token(self, d):
        return Token(d['access_token'], d.get('refresh_token') or '', time.time() + int(d.get('expires_in') or 0))

    async def poll(self, http, code):
        """Один опрос: Token, если вход подтверждён; None — ещё ждём. LoginError — не вышло."""
        try:
            async with http.post(self.oauth + '/token', data={'grant_type': 'device_code', 'code': code.device_code,
                                                             **self._auth()}) as r:
                d = await r.json(content_type=None)
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
            return None
        if d.get('access_token'):
            return self._token(d)
        err = d.get('error')
        if err == 'slow_down':
            code.interval += 5      # Яндекс просит спрашивать реже (RFC 8628): +5 с к паузе
            return None
        if err == 'authorization_pending':
            return None
        reason = {'invalid_grant': 'expired', 'access_denied': 'denied'}.get(err, 'other')
        raise LoginError(reason, str(d.get('error_description') or err))

    async def wait(self, http, code):
        """Ждать подтверждения до истечения кода. Token или LoginError."""
        while time.time() < code.expires_at:
            await asyncio.sleep(max(code.interval, 2))   # interval растёт, если Яндекс просит реже
            token = await self.poll(http, code)
            if token:
                return token
        raise LoginError('expired', 'время на ввод кода вышло')

    async def refresh(self, http, refresh_token):
        """Новый токен по refresh-токену или None."""
        try:
            async with http.post(self.oauth + '/token', data={'grant_type': 'refresh_token',
                                                             'refresh_token': refresh_token, **self._auth()}) as r:
                d = await r.json(content_type=None)
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
            return None
        return self._token(d) if d.get('access_token') else None


@dataclass
class Light:
    id: str
    name: str
    room: str = ''
    model: str = None          # 'hsv' / 'rgb' — цветная; None — только белый
    tmin: int = None           # диапазон белого, K
    tmax: int = None
    brightness: bool = False   # можно ли менять яркость
    state: list = field(default_factory=list)   # текущее состояние — действия, чтобы вернуть как было

    @property
    def color(self):
        return self.model in ('hsv', 'rgb')


def _parse_light(dev, rooms):
    l = Light(dev['id'], dev.get('name') or 'Лампа', rooms.get(dev.get('room'), ''))
    for c in dev.get('capabilities', []):
        p = c.get('parameters') or {}
        st = c.get('state')
        if c.get('type') == 'devices.capabilities.color_setting':
            l.model = p.get('color_model')
            if p.get('temperature_k'):
                l.tmin, l.tmax = p['temperature_k'].get('min', 2000), p['temperature_k'].get('max', 9000)
        elif c.get('type') == 'devices.capabilities.range' and p.get('instance') == 'brightness':
            l.brightness = True
        if st and c.get('type') in ('devices.capabilities.color_setting', 'devices.capabilities.range',
                                    'devices.capabilities.on_off'):
            if c['type'] == 'devices.capabilities.range' and st.get('instance') != 'brightness':
                continue
            l.state.append({'type': c['type'], 'state': {'instance': st['instance'], 'value': st['value']}})
    # включить/выключить — последним, иначе выключенная лампа включится от смены цвета
    l.state.sort(key=lambda a: a['type'] == 'devices.capabilities.on_off')
    return l


def light_actions(light, on=True, hsv=None, kelvin=None, brightness=None):
    """Действия для лампы. hsv=(тон 0..360, насыщенность 0..1); kelvin — белый; brightness — 1..100.
    Цветная лампа получает цвет в своём формате; лампа только с белым — kelvin (если задан)."""
    if not on:
        return [{'type': 'devices.capabilities.on_off', 'state': {'instance': 'on', 'value': False}}]
    acts = [{'type': 'devices.capabilities.on_off', 'state': {'instance': 'on', 'value': True}}]
    if hsv is not None and light.model == 'hsv':
        acts.append({'type': 'devices.capabilities.color_setting',
                     'state': {'instance': 'hsv', 'value': {'h': int(hsv[0]) % 360, 's': round(hsv[1] * 100), 'v': 100}}})
    elif hsv is not None and light.model == 'rgb':
        r, g, b = colorsys.hsv_to_rgb(hsv[0] / 360, hsv[1], 1)
        acts.append({'type': 'devices.capabilities.color_setting',
                     'state': {'instance': 'rgb', 'value': (round(r * 255) << 16) | (round(g * 255) << 8) | round(b * 255)}})
    elif kelvin is not None and light.tmin:
        acts.append({'type': 'devices.capabilities.color_setting',
                     'state': {'instance': 'temperature_k', 'value': max(light.tmin, min(light.tmax, int(kelvin)))}})
    if brightness is not None and light.brightness:
        acts.append({'type': 'devices.capabilities.range',
                     'state': {'instance': 'brightness', 'value': max(1, min(100, int(brightness)))}})
    return acts


class SmartHome:
    """API Умного дома для пользовательских приложений."""

    def __init__(self, token, iot=IOT, timeout=8):
        self.token, self.iot, self.timeout = token, iot, timeout

    async def _call(self, http, method, path, body=None):
        try:
            async with http.request(method, self.iot + path, json=body, headers={'Authorization': 'Bearer ' + self.token},
                                    timeout=aiohttp.ClientTimeout(total=self.timeout)) as r:
                if r.status == 401:
                    raise AuthError('вход больше не действует')
                d = await r.json(content_type=None)
                if r.status != 200 or d.get('status') != 'ok':
                    raise YandexError(str(d.get('message') or r.status))
                return d
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as e:
            raise YandexError(f'Умный дом не отвечает: {e}') from None

    async def user_info(self, http):
        return await self._call(http, 'GET', '/v1.0/user/info')

    async def lights(self, http):
        d = await self.user_info(http)
        rooms = {r['id']: r.get('name', '') for r in d.get('rooms', [])}
        return [_parse_light(dev, rooms) for dev in d.get('devices', [])
                if str(dev.get('type', '')).startswith('devices.types.light')]

    async def actions(self, http, per_device):
        """per_device: {device_id: [действия]}. Возвращает {device_id: код ошибки} для устройств,
        которые не выполнили команду (например, 'DEVICE_UNREACHABLE' — лампа без сети или обесточена).
        Пустой словарь — всё выполнено. Яндекс отвечает «ok» на весь запрос, даже когда часть ламп
        не послушалась, поэтому ошибки по каждой лампе нужно смотреть здесь."""
        body = {'devices': [{'id': k, 'actions': v} for k, v in per_device.items() if v]}
        if not body['devices']:
            return {}
        d = await self._call(http, 'POST', '/v1.0/devices/actions', body)
        return _action_errors(d)


def _action_errors(d):
    """{device_id: код ошибки} из ответа на /devices/actions."""
    failed = {}
    for dev in d.get('devices') or []:
        results = [dev.get('action_result') or {}]
        results += [(c.get('state') or {}).get('action_result') or {} for c in dev.get('capabilities') or []]
        for res in results:
            if res.get('status') == 'ERROR':
                failed[dev.get('id')] = res.get('error_code') or 'ERROR'
                break
    return failed
