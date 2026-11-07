import os
import sys
import unittest

import aiohttp
from aiohttp import web

sys.path.insert(0, os.path.dirname(__file__))
import fake_yandex  # noqa: E402
from yandex_smarthome import (AuthError, DeviceLogin, LoginError, SmartHome, light_actions)  # noqa: E402


class SmartHomeTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.app = fake_yandex.make_app(confirm_after=1.0)
        self.runner = web.AppRunner(self.app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, '127.0.0.1', 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        self.url = f'http://127.0.0.1:{port}'
        self.http = aiohttp.ClientSession()
        self.login = DeviceLogin('test-client', 'test-secret', oauth=self.url)

    async def asyncTearDown(self):
        await self.http.close()
        await self.runner.cleanup()

    async def test_login_and_lights(self):
        code = await self.login.start(self.http, device_id='dev-1', device_name='Проверка')
        self.assertEqual(code.user_code, 'abcd1234')
        self.assertIsNone(await self.login.poll(self.http, code))      # ещё не подтвердили
        token = await self.login.wait(self.http, code)
        self.assertEqual((token.access, token.refresh), ('AT1', 'RT1'))
        home = SmartHome(token.access, iot=self.url)
        lights = await home.lights(self.http)
        self.assertEqual([l.id for l in lights], ['lamp-1', 'lamp-2'])  # розетка не свет
        l1, l2 = lights
        self.assertEqual((l1.room, l1.model, l1.color, l1.brightness), ('Гостиная', 'hsv', True, True))
        self.assertEqual((l2.model, l2.color, l2.tmin, l2.tmax), (None, False, 2700, 6500))
        self.assertEqual(l2.state[-1]['type'], 'devices.capabilities.on_off')   # включение — последним

        await home.actions(self.http, {l1.id: light_actions(l1, hsv=(15, 0.9), brightness=80),
                                       l2.id: light_actions(l2, hsv=(15, 0.9), kelvin=9000, brightness=500)})
        lamps = self.app['st']['lamps']
        self.assertEqual(lamps['lamp-1']['hsv'], {'h': 15, 's': 90, 'v': 100})
        self.assertEqual((lamps['lamp-2']['temperature_k'], lamps['lamp-2']['brightness'], lamps['lamp-2']['on']), (6500, 100, True))

        await home.actions(self.http, {l1.id: l1.state, l2.id: l2.state})   # вернуть как было
        self.assertEqual((lamps['lamp-2']['on'], lamps['lamp-2']['temperature_k'], lamps['lamp-1']['brightness']), (False, 3000, 60))

    async def test_refresh_and_auth_error(self):
        token = await self.login.refresh(self.http, 'RT1')
        self.assertEqual(token.access, 'AT2')
        self.assertIsNone(await self.login.refresh(self.http, 'bad'))
        with self.assertRaises(AuthError):
            await SmartHome('wrong', iot=self.url).lights(self.http)

    async def test_wrong_secret(self):
        bad = DeviceLogin('test-client', 'nope', oauth=self.url)
        code = await bad.start(self.http)
        with self.assertRaises(LoginError):
            await bad.poll(self.http, code)

    def test_rgb_actions(self):
        from yandex_smarthome import Light
        acts = light_actions(Light('x', 'X', model='rgb', brightness=True), hsv=(0, 1.0), brightness=50)
        self.assertEqual(acts[1]['state'], {'instance': 'rgb', 'value': 0xff0000})
        self.assertEqual(light_actions(Light('x', 'X'), on=False)[0]['state']['value'], False)


if __name__ == '__main__':
    unittest.main()
