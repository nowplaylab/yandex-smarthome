"""Подставной Яндекс для проверок: вход по коду (oauth) и Умный дом (iot) с двумя лампами и розеткой."""
import time

from aiohttp import web


def make_app(confirm_after=1.0, client_id='test-client', client_secret='test-secret', slow_down=0):
    # offline — лампы «без сети»: Яндекс отвечает status ok, но по такой лампе action_result ERROR.
    # slow_down — сколько первых опросов ответить «спрашивайте реже».
    st = {'code_at': 0, 'log': [], 'offline': set(), 'slow_down': slow_down, 'polls': []}
    lamps = {
        'lamp-1': {'name': 'Лампочка', 'room': 'r1', 'model': 'hsv', 'on': True, 'hsv': None, 'temperature_k': 4000, 'brightness': 60},
        'lamp-2': {'name': 'Потолок', 'room': 'r2', 'model': None, 'on': False, 'hsv': None, 'temperature_k': 3000, 'brightness': 40},
    }
    st['lamps'] = lamps

    def client_ok(d):
        return d.get('client_id') == client_id and d.get('client_secret') == client_secret

    async def device_code(r):
        d = await r.post()
        if d.get('client_id') != client_id:
            return web.json_response({'error': 'invalid_client'}, status=400)
        st['code_at'] = time.time()
        return web.json_response({'device_code': 'DC1', 'user_code': 'abcd1234', 'verification_url': 'https://oauth.yandex.ru/device',
                                  'interval': 1, 'expires_in': 30})

    async def token(r):
        d = await r.post()
        if not client_ok(d):
            return web.json_response({'error': 'invalid_client', 'error_description': 'Wrong client secret'}, status=400)
        if d.get('grant_type') == 'device_code':
            st['polls'].append(time.time())
            if st['slow_down'] > 0:
                st['slow_down'] -= 1
                return web.json_response({'error': 'slow_down'}, status=400)
            if d.get('code') != 'DC1':
                return web.json_response({'error': 'invalid_grant'}, status=400)
            if time.time() - st['code_at'] < confirm_after:
                return web.json_response({'error': 'authorization_pending'}, status=400)
            return web.json_response({'access_token': 'AT1', 'refresh_token': 'RT1', 'expires_in': 31536000})
        if d.get('grant_type') == 'refresh_token' and d.get('refresh_token') == 'RT1':
            return web.json_response({'access_token': 'AT2', 'refresh_token': 'RT2', 'expires_in': 31536000})
        return web.json_response({'error': 'invalid_grant'}, status=400)

    def authed(r):
        return r.headers.get('Authorization', '') in ('Bearer AT1', 'Bearer AT2')

    async def user_info(r):
        if not authed(r):
            return web.json_response({'status': 'error', 'message': 'unauthorized'}, status=401)
        devs = []
        for i, l in lamps.items():
            caps = [{'type': 'devices.capabilities.on_off', 'parameters': {'split': False}, 'state': {'instance': 'on', 'value': l['on']}},
                    {'type': 'devices.capabilities.range', 'parameters': {'instance': 'brightness', 'range': {'min': 1, 'max': 100}},
                     'state': {'instance': 'brightness', 'value': l['brightness']}}]
            params = {'temperature_k': {'min': 2700, 'max': 6500}}
            if l['model']:
                params['color_model'] = l['model']
            cstate = ({'instance': 'hsv', 'value': l['hsv']} if l['hsv'] else {'instance': 'temperature_k', 'value': l['temperature_k']})
            caps.append({'type': 'devices.capabilities.color_setting', 'parameters': params, 'state': cstate})
            devs.append({'id': i, 'name': l['name'], 'type': 'devices.types.light', 'room': l['room'], 'capabilities': caps})
        devs.append({'id': 'sock', 'name': 'Розетка', 'type': 'devices.types.socket', 'capabilities': []})
        return web.json_response({'status': 'ok', 'rooms': [{'id': 'r1', 'name': 'Гостиная'}, {'id': 'r2', 'name': 'Спальня'}],
                                  'devices': devs})

    async def actions(r):
        if not authed(r):
            return web.json_response({'status': 'error', 'message': 'unauthorized'}, status=401)
        body = await r.json()
        out = []
        for d in body['devices']:
            if d['id'] in st['offline']:
                err = {'status': 'ERROR', 'error_code': 'DEVICE_UNREACHABLE', 'error_message': 'устройство недоступно'}
                out.append({'id': d['id'], 'capabilities': [
                    {'type': a['type'], 'state': {'instance': a['state']['instance'], 'action_result': err}}
                    for a in d['actions']]})
                continue
            out.append({'id': d['id'], 'capabilities': [
                {'type': a['type'], 'state': {'instance': a['state']['instance'], 'action_result': {'status': 'DONE'}}}
                for a in d['actions']]})
            l = lamps[d['id']]
            for a in d['actions']:
                inst, val = a['state']['instance'], a['state']['value']
                if inst == 'on':
                    l['on'] = val
                elif inst == 'hsv':
                    l['hsv'] = val
                elif inst == 'temperature_k':
                    l['temperature_k'], l['hsv'] = val, None
                elif inst == 'brightness':
                    l['brightness'] = val
        st['log'].append(body)
        return web.json_response({'status': 'ok', 'request_id': 'req-1', 'devices': out})

    app = web.Application()
    app.add_routes([web.post('/device/code', device_code), web.post('/token', token),
                    web.get('/v1.0/user/info', user_info), web.post('/v1.0/devices/actions', actions)])
    app['st'] = st
    return app
