"""
Copyright © 2015 Constverum <constverum@gmail.com>. All rights reserved.

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
"""

# TODO:::: HTTP_PROXY_AUTHORIZATION

# HTTP_X_FORWARDED_FOR придуман программистами при разработке Squid.
# Програмисты Microsoft в своем ISA Server используют HTTP_X_REMOTE_ADDR.
# Другие прокси также юзают такие заголовки:
# HTTP_RLNCLIENTIPADDR, HTTP_REMOTE_HOST_WP, HTTP_CLIENTADDRESS, HTTP_CLIENT_ADDRESS,
# HTTP_X_ORIGINAL_HOST, HTTP_X_ORIGINAL_REMOTE_ADDR, HTTP_XXX_REAL_IP, HTTP_X_ORIG_CLIENT,
# HTTP_X_CISCO_BBSM_CLIENTIP, HTTP_X_AZC_REMOTE_ADDR, HTTP_X_ORIGINAL_REMOTE_ADDR, HTTP_CLIENT_IP
# HTTP_X_PROXY_ID

import re
import sys
import ssl
import time
import json
import socket
import struct
import aiohttp
import asyncio
import logging
import urllib.request
import urllib.parse
import pprint
from collections import defaultdict, Counter, Iterable

import random
from functools import partial

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(message)s',
    datefmt='[%H:%M:%S]',
    level=logging.DEBUG)
log = logging.getLogger('ProxyChecker')


MaxConcurrentConnections = asyncio.Semaphore(200)


def connector(negotiator_func):
    async def wrapper(self, p, sem):
        result = False
        attempt = 0
        while attempt < self.attemptsConnect:
            with (await MaxConcurrentConnections):
                attempt += 1
                et = p.expected_type
                firstCharNgtr = p.ngtr[0]
                if et and attempt > 1 and et != firstCharNgtr:  # 'S' or 'H'
                    # result = False
                    p.log('Expected another proxy type')
                    # break
                # with (await sem):
                try:
                    await self.connect_to_proxy(p)
                except ProxyTimeoutError:
                    continue
                except ProxyConnError:
                    break
                    # continue
                result = await negotiator_func(self, p, sem)
                p.writer.close()
                p.log('Connection: closed')
                if result is None:
                    continue
                elif result is True:
                    p.expected_type = firstCharNgtr
                    p.log('Set expected proxy type: %s' % firstCharNgtr)
                    break
                else:
                    break
        return result or False
    return wrapper


class ProxyError(Exception):
    pass
    # def __init__(self, msg):
    #     self.args = (msg,)
    #     self.msg = msg


class ProxyConnError(ProxyError):
    pass


class ProxyRecvError(ProxyError):
    pass


class ProxyTimeoutError(ProxyError):
    pass


class ProxyEmptyRecvError(ProxyError):
    pass


class Proxy:
    _log = defaultdict(list)
    _errors = defaultdict(Counter)
    _expectedType = dict()
    # _runtimes = defaultdict(list)
    _anonymity = defaultdict(dict)

    def __init__(self, host, port, ptype=[], ngtr=None, judge=None):
        self.host = host
        self.port = int(port)
        if isinstance(ptype, str):
            self.ptype = ptype.split(',')
        elif isinstance(ptype, (list, tuple)):
            self.ptype = ptype
        else:
            raise ValueError('Proxy type can be a string or list')
        self.ngtr = ngtr
        self.judge = judge
        self.country = None
        self.errors = self._errors[self.host]
        self.isWorking = None
        self.reader = None
        self.writer = None
        # self.geo = {'city': {'code': None, 'name': None},
        #             'country': {'code': None, 'name': None}}

    def check_on_errors(self, msg):
        err = None
        if 'timeout' in msg:
            err = 'timeout'
        elif 'Connection: failed' in msg:
            err = 'connection_failed'
        elif 'Received: failed' in msg:
            err = 'connection_is_reset'
        elif 'Received: 0 bytes' in msg:
            err = 'empty_response'
        elif 'SSL: UNKNOWN_PROTOCOL' in msg:
            err = 'ssl_unknown_protocol'
        elif 'SSL: CERTIFICATE_VERIFY_FAILED' in msg:
            err = 'ssl_verified_failed'

        if err:
            self._errors[self.host][err] += 1

    @property
    def anonymity(self):
        return self._anonymity.get(self.host)

    @anonymity.setter
    def anonymity(self, lvl):
        self._anonymity[self.host][self.ngtr] = lvl

    @property
    def expected_type(self):
        return self._expectedType.get(self.host, False)

    @expected_type.setter
    def expected_type(self, t):
        self._expectedType[self.host] = t

    def log(self, msg=None, stime=None):
        if msg:
            runtime = time.time()-stime if stime else 0
            log.debug('{host}: {ngtr}: {msg}; Runtime: {rt:.4f}'.format(
                host=self.host, ngtr=self.ngtr, msg=msg, rt=runtime))
            msg = '{ngtr}: {msg:.58s}{trunc}'.format(
                ngtr=self.ngtr, msg=msg, trunc='...' if len(msg) > 58 else '')
            self._log[self.host].append((msg, runtime))
            self.check_on_errors(msg)
            # if runtime:
            #     self._runtimes[self.host].append((self.ngtr, runtime))
        else:
            return self._log[self.host]

    def __repr__(self):
        if self.ptype:
            types = ', '.join(['{}: {}'.format(pt, self.anonymity[pt])
                               for pt in self.ptype])
        else:
            types = '-'
        return '<Proxy [{types}] {host}:{port}>'.format(
            types=types, host=self.host, port=self.port)
        # <Proxy US [HTTP: Anonymous, HTTPS: High] 10.0.0.1:8080>,
        # Runtime: {avg}  avg=self.get_avg_runtime()

    # def get_avg_runtime(self):
    #     if not self.ptype:
    #         return 0
    #     runtimes = defaultdict(list)
    #     for ngtr, runtime in self._runtimes[self.host]:
    #         if ngtr in self.ptype:
    #             runtimes[ngtr].append(runtime)
    #     results = []
    #     for ngtr, runtime in runtimes.items():
    #         results.append('%s: AVG: %.2f, MAX: %.2f' % (ngtr, sum(runtime)/len(runtime), max(runtime)))
    #     # print('RUNTIME: %s: %s' % (self.host, runtimes))
    #     return results


class Judge:
    loop = asyncio.get_event_loop()
    timeout = 0

    def __init__(self, url):
        self.url = url
        self.host = urllib.parse.urlparse(url).netloc
        print('self.host: %r' % self.host, type(self.host) )
        self.path = url.split(self.host)[-1]
        self.ip = None
        self.bip = None
        self.isWorking = False

    async def set_ip(self):
        log.debug('%s: set_ip' % self.host)
        try:
            self.ip = await asyncio.wait_for(
                             self.loop.run_in_executor(
                              None, socket.gethostbyname, self.host),
                             self.timeout)
        except (socket.gaierror, asyncio.TimeoutError) as e:
            log.debug('\n\n\n%s: set_ip ERROR: %s' % (self.host, e))
            return
        # print('%s: set_ip ip: %s' % (self.host, self.ip))
        self.bip = socket.inet_aton(self.ip)
        # print('%s: set_ip bip: %s' % (self.host, self.bip))

    async def check_response(self, myip):
        # log.debug('%s: check_response; timeout: %d' % (self.host, self.timeout))
        try:
            # resp = await asyncio.wait_for(
            #         self.loop.run_in_executor(
            #                 None, partial(urllib.request.urlopen, url=self.url, timeout=self.timeout)),
            #         self.timeout)
            log.debug('%s: request.urlopen;' % self.host)
            resp = await self.loop.run_in_executor(
                            None,
                            partial(urllib.request.urlopen,
                                    url=self.url,
                                    timeout=self.timeout))
            # resp = await aiohttp.request('GET', self.url)
            # data = await resp.read()
            # data = data.lower()
        except (urllib.error.HTTPError, socket.timeout) as e:  # asyncio.TimeoutError
            log.debug('\n\n\n%s: check_response ERROR: %s' % (self.host, e))
            # return
        else:
            log.debug('%s: check_response STATUS: %s' % (self.host, resp.status))
            if resp.status == 200:
                self.isWorking = True if myip in resp.read().lower() else False

    def __repr__(self):
        return '<Judge {host}>'.format(host=self.host)


class ProxyChecker:
    def __init__(self,
                 proxies=[],
                 timeout=6,
                 connects=3,
                 sslverify=False,
                 judges=[],
                 showstats=False):  # dest=None, **kwargs
        if not proxies:
            raise ValueError('You must set proxy list in <proxies> var')
        if not isinstance(judges, Iterable):
            raise ValueError('<judges> var must be a iterable type of urls')

        self.proxies = dict(input=proxies, clean=[], good=[], bad=[])
        self.timeout = timeout  # in seconds
        self.sslContext = True if sslverify else ssl._create_unverified_context()
        self.attemptsConnect = connects
        self.showStats = showstats
        self.judges = judges

        # self.dest = {'host': 'httpbin.org',
        #                      'port': 80,
        #                      'page': '/get?show_env',
        #                      'length': -1,
        #                      'search': 'headers'}
        # addr = socket.inet_aton(
        #            socket.gethostbyname(
        #                self.dest['host']))  # b'J}\x8fj'
        # defua = 'Mozilla/5.0 (Windows NT 6.1; WOW64; rv:41.0)'\
        #         'Gecko/20100101 Firefox/41.0'

        # self.requests = dict(
        #     SOCKS4=struct.pack('>2BH5B', 4, 1, 80, *bip, 0),
        #     SOCKS5=[struct.pack('3B', 5, 1, 0),
        #             struct.pack('>8BH', 5, 1, 0, 1, *bip, 80)],
        #     CONNECT='CONNECT {host}:{port} HTTP/1.1\r\nHost: {host}\r\n'
        #             'Connection: keep-alive\r\n\r\n'.format(
        #                 host=self.dest['host'], port=443).encode(),
        #     GET='GET {page} HTTP/1.1\r\nHost: {host}\r\nAccept: *.*, */*\r\nConnection: close\r\n\r\n')

        self.ngtrs = [
            {'name': 'SOCKS4', 'fn': self.negotiate_SOCKS4},
            {'name': 'HTTP', 'fn': self.negotiate_HTTP},
            {'name': 'SOCKS5', 'fn': self.negotiate_SOCKS5},
            {'name': 'HTTPS', 'fn': self.negotiate_HTTPS}]
        self.ippattern = re.compile(
            b'(?:(?:25[0-5]|2[0-4]\d|[01]?\d\d?)\.){3}'
            b'(?:25[0-5]|2[0-4]\d|[01]?\d\d?)')

    def start(self):
        self.loop = asyncio.get_event_loop()
        self.loop.run_until_complete(self.run())
        self.loop.close()

    async def run(self):
        self.myRealIP = self.get_my_ip()

        await self.check_judges()
        await self.clear_proxies()

        stime = time.time()

        # await asyncio.wait([self.check_proxy(p) for p in proxies])
        await asyncio.gather(*[
            asyncio.ensure_future(self.check_proxy(p))
            for p in self.proxies['clean']])

        log.debug('Complete: %.2f sec', time.time()-stime)

        if self.showStats:
            self.show_stats(self.proxies['clean'])

        # self.tasks = asyncio.Queue()
        # for p in self.proxies['input']:
        #     self.tasks.put(asyncio.ensure_future(self.check_proxy(p)))
        #     log.debug('Check %d proxies', self.tasks.qsize())
        # while True:
        #     task = await self.tasks.get()
        #     print('task:', task)
        #     await task

    # def get_host_by_name(self, name):
    #         return self.loop.run_in_executor(
    #                 None, socket.gethostbyname, name)

    async def check_judges(self):
        log.debug('Start check judges')
        stime = time.time()
        Judge.timeout = self.timeout
        judges = [Judge(url=url) for url in self.judges]

        await asyncio.gather(*(asyncio.ensure_future(j.set_ip())
                             for j in judges))
        await asyncio.gather(*(asyncio.ensure_future(
            j.check_response(myip=self.myRealIP))
            for j in judges))

        self.judges = [j for j in judges if j.isWorking]

        log.debug('End check judges. Runtime: %.4f;\nJudges: %s' % (
            time.time()-stime, self.judges))

    async def clear_proxies(self):
        proxies = [Proxy(*p, judge=random.choice(self.judges))
                   for p in self.proxies['input']]

        # Remove duplicates
        proxies = await asyncio.gather(*[
            asyncio.ensure_future(
                self.resolve_host(p))
            for p in proxies])
        self.proxies['clean'] = {p.host: p for p in proxies if p}.values()

        log.debug('Stat: Received: %d / Unique: %d',
                  len(self.proxies['input']), len(self.proxies['clean']))

    async def resolve_host(self, p):
        name = p.host
        stime = time.time()
        try:
            p.host = await self.loop.run_in_executor(
                            None, socket.gethostbyname, p.host)
            msg = 'Host resolved (orig. name: %s)' % name
        except socket.gaierror:
            msg = 'Could not resolve host'
            return None
        else:
            return p
        finally:
            p.log(msg, stime)

    def get_good_proxies(self, ptype=None):
        if not ptype:
            return self.proxies['good']

        selectTypes = []
        if isinstance(ptype, str):
            selectTypes.append(ptype)
        elif isinstance(ptype, list):
            selectTypes.extend(ptype)
        else:
            raise ValueError('<ptype> var must be a list of'
                             'the types or a string of the type')
        res = []
        for sType in selectTypes:
            for proxy in self.proxies['good']:
                if sType.upper() in proxy.ptype:
                    res.append(proxy)
        return res

    def get_my_ip(self):
        url = 'http://httpbin.org/get?show_env'
        data = urllib.request.urlopen(url)
        data = json.loads(data.read().decode())
        ip = data['origin']
        headers = data['headers']
        log.debug('IP: %s;\nHeaders: %s;' % (ip, headers))
        return ip.encode()

    def get_request(self, p, name):  # ngtr, host=None, bip=None
        if name == 'SOCKS5-Init':
            req = struct.pack('3B', 5, 1, 0)
        elif name == 'SOCKS5-Conn':
            req = struct.pack('>8BH', 5, 1, 0, 1, *p.judge.bip, 80)
        elif name == 'SOCKS4':
            req = struct.pack('>2BH5B', 4, 1, 80, *p.judge.bip, 0)
        elif name == 'CONNECT':
            req = ('CONNECT {host}:80 HTTP/1.1\r\nHost: {host}\r\n'
                   'Connection: keep-alive\r\n\r\n').format(
                    host=p.judge.host).encode()
        elif name == 'GET':
            req = ('GET {page} HTTP/1.1\r\nHost: {host}\r\n'
                   'Accept: *.*, */*\r\nConnection: close\r\n\r\n')
        return req

    async def check_proxy(self, p):
        ngtrs = []
        if p.ptype:
            ngtrs = [n for n in self.ngtrs if n['name'] in p.ptype]
        else:
            ngtrs = self.ngtrs[:]
            # ngtrs = [{'name': 'HTTP', 'fn': self.negotiate_HTTP}]
            # ngtrs = [
            #     {'name': 'SOCKS4', 'fn': self.negotiate_SOCKS4},
            #     {'name': 'HTTP', 'fn': self.negotiate_HTTP},
            #     {'name': 'SOCKS5', 'fn': self.negotiate_SOCKS5},
            # ]

        # sem = asyncio.Semaphore(1)
        results = await asyncio.gather(*[
                    asyncio.ensure_future(
                      n['fn'](p=Proxy(p.host, p.port, ngtr=n['name'], judge=p.judge), sem=None))
                    for n in ngtrs])
        okTypes = [ngtrs[i]['name'] for i, r in enumerate(results) if r]
        if okTypes:
            p.ptype = okTypes[:]
            p.isWorking = True
        else:
            p.ptype = None
            p.isWorking = False

        self.proxies['good' if p.isWorking else 'bad'].append(p)
        # p.log('Status: %s' % p.isWorking)

    async def connect_to_proxy(self, p):
        p.log('Initial connection')
        stime = time.time()
        try:
            p.reader, p.writer = await asyncio.wait_for(
                    asyncio.open_connection(p.host, p.port),
                    timeout=self.timeout)
        except asyncio.TimeoutError:
            msg = 'Connection: timeout'
            raise ProxyTimeoutError(msg)
        except (ConnectionRefusedError, OSError):
            msg = 'Connection: failed'
            raise ProxyConnError(msg)
        else:
            msg = 'Connection: success'
        finally:
            p.log(msg, stime)

    async def send(self, p, req):
        p.log('Request: %s' % (req))
        p.writer.write(req)
        await p.writer.drain()
        # await asyncio.sleep(0.5)

    async def recv(self, p, length):
        resp = ''
        stime = time.time()
        try:
            resp = await asyncio.wait_for(
                p.reader.read(length), timeout=self.timeout)
            msg = 'Received: %s bytes;' % len(resp)
        except asyncio.TimeoutError:
            msg = 'Received: timeout;'
            raise ProxyTimeoutError(msg)
        except (ConnectionResetError, OSError):
            # OSError - [Errno 9] Bad file descriptor
            msg = 'Received: failed;'  # (connection is reset by the peer)
            raise ProxyRecvError(msg)
        else:
            if not resp:
                # p.log('Failed (empty response)')
                raise ProxyEmptyRecvError(msg)
        finally:
            if resp:
                msg += '::: %s' % resp[:12]
            p.log(msg, stime)
            # log.debug('%s: Response: %r', p.host, resp)
        return resp

    async def check_get_request(self, p, scheme):
        _dest = dict(host=p.judge.host, page=p.judge.path)
        if p.ngtr == 'HTTP':
            # set full uri for HTTP-negotiator
            _dest['page'] = 'http://{host}{page}'.format(**_dest)

        await self.send(p,
                        self.get_request(p, 'GET').format(**_dest).encode())
        stime = time.time()

        try:
            resp = await self.recv(p, -1) # self.dest['length']
        except (ProxyTimeoutError, ProxyRecvError):
            return
        except ProxyEmptyRecvError:
            if p.ngtr == 'HTTP':
                return False
            else:
                return

        # print('resp before decode: %r', resp)
        # resp = resp.decode()
        # print('resp after decode: %r', resp)
        httpStatusCode = resp[9:12]
        if httpStatusCode == b'200':
            # and self.dest['search'].encode() in resp
            self.check_anonymity(p, resp)
            p.log('Get: success', stime)
            return True
        else:
            # p.log('%s: resp::: %s' % (ngtr, resp))
            p.log('Get: failed; Resp: %s' % resp, stime)
            return False

    def check_anonymity(self, p, resp):
        # try:
        #     resp = resp.decode()
        # except UnicodeDecodeError:
        #     raise e
        resp = resp.lower()
        pprint.pprint(resp)
        ipList = self.ippattern.findall(resp)
        # via = body.find('HTTP_VIA')
        # proxyword = body.lower().find('proxy')
        print('%s: ipList::: %s' % (p.host, ipList))
        print('%s: via: %s; proxy: %s' % (p.host, b'via' in resp, b'proxy' in resp))
        if self.myRealIP in ipList:
            p.anonymity = 'Transparent'
            # print('Transparent')
        elif b'via' in resp or b'proxy' in resp:
            p.anonymity = 'Anonymous'
            # print('Anonymous')
        else:
            p.anonymity = 'High'
            # print('High')

        # _, body = resp.decode().split('\r\n\r\n')
        # data = json.loads(body)
        # ipList = data['origin'].split(', ')
        # via = data['headers'].get('Via')
        # proxyAuth = data['headers'].get('Proxy-Authorization')

        # if self.myRealIP in ipList:
        #     p.anonymity = 'Transparent'
        # elif via or proxyAuth:
        #     p.anonymity = 'Anonymous'
        # else:
        #     p.anonymity = 'High'

    @connector
    async def negotiate_SOCKS5(self, p, sem):
        # await self.send(p, self.requests['SOCKS5'][0])
        await self.send(p, self.get_request(p, 'SOCKS5-Init'))

        try:
            resp = await self.recv(p, 2)
        except (ProxyTimeoutError, ProxyRecvError):
            return
        except ProxyEmptyRecvError:
            return False

        if resp[0] == 0x05 and resp[1] == 0xff:
            p.log('Failed (auth is required)')
            return False
        elif resp[0] != 0x05 or resp[1] != 0x00:
            p.log('Failed (invalid data)')
            return False
        else:
            # p.log('SOCKS5: success (auth is not required)')
            # await self.send(p, self.requests['SOCKS5'][1])
            await self.send(p, self.get_request(p, 'SOCKS5-Conn'))

            try:
                resp = await self.recv(p, 10)
            except (ProxyTimeoutError, ProxyRecvError, ProxyEmptyRecvError):
                return

            if resp[0] != 0x05 or resp[1] != 0x00:
                p.log('Failed (invalid data)')
            else:
                p.log('Request granted')
                result = await self.check_get_request(p, scheme='HTTP')
        return result

    @connector
    async def negotiate_SOCKS4(self, p, sem):
        await self.send(p, self.get_request(p, 'SOCKS4'))

        try:
            resp = await self.recv(p, 8)
        except (ProxyTimeoutError, ProxyRecvError):
            return
        except ProxyEmptyRecvError:
            return False

        if resp[0] != 0x00 or resp[1] != 0x5A:
            p.log('Failed (invalid data)')
            return False
        # resp = b'\x00Z\x00\x00\x00\x00\x00\x00' // ord('Z') == 90 == 0x5A
        else:
            p.log('Request granted')
            result = await self.check_get_request(p, scheme='HTTP')
        return result

    @connector
    async def negotiate_HTTPS(self, p, sem):
        await self.send(p, self.get_request(p, 'CONNECT'))

        try:
            resp = await self.recv(p, 128)
        except (ProxyTimeoutError, ProxyRecvError):
            return
        except ProxyEmptyRecvError:
            return False

        httpStatusCode = resp[9:12]
        if httpStatusCode != b'200':
            p.log('Failed (error)')
            return False
        else:
            # sock = p.writer.get_extra_info('socket')
            # stime = time.time()
            # try:
            #     # like aiohttp/connector.py ProxyConnector._create_connection()
            #     p.reader, p.writer = await asyncio.wait_for(
            #         asyncio.open_connection(
            #             ssl=self.sslContext, sock=sock,
            #             server_hostname=self.dest['host']),
            #         timeout=self.timeout)
            #     msg = 'SSL: enabled'
            # except ConnectionResetError:
            #     msg = 'SSL: failed'
            #     return
            # except asyncio.TimeoutError:
            #     msg = 'SSL: timeout'
            #     return
            # except ssl.SSLError as e:
            #     msg = 'SSL: %s' % e
            #     return False
            # finally:
            #     p.log(msg, stime)
            result = await self.check_get_request(p, scheme='HTTPS')
        return result

    @connector
    async def negotiate_HTTP(self, p, sem):
        result = await self.check_get_request(p, scheme='HTTP')
        return result

    def show_stats(self, proxies):
        errors = Counter()
        stat = {'Connection failed': [],
                'Connection timeout': [],
                'Connection success': []}

        for p in sorted(proxies, key=lambda p: p.host):
            errors.update(p.errors)
            msgs = [l[0] for l in p.log()]

            if 'Connection: success' in ' '.join(msgs):
                print(p)
                stat['Connection success'].append(p)
                events_by_ngtr = defaultdict(list)
                for event, runtime in p.log():
                    if 'Host resolved' in event:
                        print('\t{:<70} Runtime: {:.4f}'.format(
                            event.replace('None: ', ''), runtime))
                    else:
                        ngtrChars = event.find(':')
                        ngtr = event[:ngtrChars]
                        # event = 'SOCKS5: Connection: success' =>
                        # ngtr = 'SOCKS5'
                        event = event[ngtrChars+2:]
                        events_by_ngtr[ngtr].append((event, runtime))

                for ngtr, events in sorted(events_by_ngtr.items(),
                                           key=lambda item: item[0]):
                    print('\t%s' % ngtr)
                    for event, runtime in events:
                        if 'Initial connection' in event:
                            continue
                        elif 'Connection:' in event and\
                             'Connection: closed' not in event:
                            print('\t\t{:<66} Runtime: {:.4f}'.format(event, runtime))
                        else:
                            print('\t\t\t{:<62} Runtime: {:.4f}'.format(event, runtime))
            elif 'Connection: failed' in ' '.join(msgs):
                stat['Connection failed'].append(p)
            else:
                stat['Connection timeout'].append(p)
        pprint.pprint(stat)
        print('Errors:', errors)


def main():
    testProxyList = '''
    119.47.91.113:1080
    41.60.130.36:1080
    82.208.95.64:1080
    202.22.195.197:1080
    201.210.227.21:1080
    187.210.37.36:1080
    219.157.77.102:1080
    c-76-124-41-9.hsd1.pa.comcast.net:53641
    77.242.22.254:8741
    110.169.152.251:1080
    thetru2072.lnk.telstra.net:35067
    66.188.42.61:32282
    62.73.97.139:5224
    cpe-217-30-196-65.enet.vn.ua:5600
    180.250.159.19:1080
    vmi15075.contabo.host:3128
    120.195.199.85:80
    static-a68.ekaterinburg.golden.ru:3128
    sophoswebtest.ucps.k12.nc.us:8080
    sophosweb7.ucps.k12.nc.us:8080
    sophosweb4.ucps.k12.nc.us:8080
    sophosweb3.ucps.k12.nc.us:8080
    sophosweb2.ucps.k12.nc.us:8080
    sophosweb15.ucps.k12.nc.us:8080
    sophosweb12.ucps.k12.nc.us:8080
    sophosweb11.ucps.k12.nc.us:8080
    sophosweb10.ucps.k12.nc.us:8080
    sophosweb1.ucps.k12.nc.us:8080
    sokol-rampuse.core.ttnet.cz:8000
    skombro.rogiken.org:8080
    server192.120.itcsa.net:3128
    120.195.201.165:80
    120.195.195.36:80
    radbr.com.br:3128
    120.195.203.213:80
    ns517810.ip-192-99-20.net:3128
    ns506775.ip-198-100-148.net:3128
    ns376204.ip-5-39-81.eu:3128
    ns302694.ip-94-23-200.eu:3128
    mx2.ftnic.net:3128
    218.92.227.170:18186
    mpe-46-175.mpe.lv:8080
    mdh-17-54.tm.net.my:8080
    120.195.206.63:80
    120.195.205.48:80
    120.195.193.109:80
    mail2.jy-hydraulic.com:8089
    www5072uo.sakura.ne.jp:8080
    li634-239.members.linode.com:8080
    leased-line-225-225.telecom.by:8081
    l37-194-52-83.novotelecom.ru:8080
    ip2-232.kortedala.com:8085
    120.195.207.199:80
    120.195.205.116:80
    120.195.200.30:80
    icm-02.cc.umanitoba.ca:8080
    host136-45-231-152.movistar.com.ni:3128
    host-41-222-57-14.cybernet.co.tz:8081
    193.37.152.186:3128
    190.15.192.120:3128
    187.18.122.211:8080
    202.21.176.108:8080
    69.168.242.44:8080
    69.168.255.85:8080
    60.191.174.13:3128
    61.75.2.124:3128
    118.144.156.2:3128
    60.191.168.43:3128
    54.207.45.19:3333
    54.174.168.237:3128
    222.243.16.44:3128
    221.214.166.181:3128
    218.76.76.11:3128'''

    def test(goodProxies):
        d = dict(
            SOCKS5=['76.124.41.9'],
            SOCKS4=['41.60.130.36', '77.242.22.254', '82.208.95.64', '110.169.152.251', '119.47.91.113', '180.250.159.19', '187.210.37.36', '202.22.195.197', '219.157.77.102'],
            HTTPS=['5.39.81.72', '41.222.57.14', '54.174.168.237', '54.207.45.19', '62.204.241.146', '69.168.242.44', '69.168.255.85', '83.241.46.175', '94.23.200.49', '95.109.117.232', '120.195.193.109', '120.195.195.36', '120.195.199.85', '120.195.200.30', '120.195.201.165', '120.195.203.213', '120.195.205.116', '120.195.205.48', '120.195.206.63', '120.195.207.199', '152.26.69.30', '152.26.69.31', '152.26.69.32', '152.26.69.33', '152.26.69.36', '152.26.69.39', '152.26.69.40', '152.26.69.41', '152.26.69.42', '152.26.69.45', '152.231.45.136', '187.18.122.211', '192.99.20.92', '193.37.152.186', '198.100.148.75', '202.21.176.108', '212.119.242.68', '213.184.225.225', '218.92.227.170', '219.92.17.54'],
            HTTP=['5.39.81.72', '41.222.57.14', '54.207.45.19', '60.191.168.43', '60.191.174.13', '61.75.2.124', '62.204.241.146', '69.168.242.44', '69.168.255.85', '83.241.46.175', '94.23.200.49', '95.109.117.232', '106.186.114.239', '118.144.156.2', '120.195.193.109', '120.195.195.36', '120.195.199.85', '120.195.200.30', '120.195.201.165', '120.195.203.213', '120.195.205.116', '120.195.205.48', '120.195.206.63', '120.195.207.199', '152.231.45.136', '152.26.69.30', '152.26.69.31', '152.26.69.32', '152.26.69.33', '152.26.69.36', '152.26.69.39', '152.26.69.40', '152.26.69.41', '152.26.69.42', '152.26.69.45', '187.18.122.211', '190.15.192.120', '192.99.20.92', '193.37.152.186', '198.100.148.75', '202.21.176.108', '210.51.48.206', '212.119.242.68', '213.184.225.225', '218.76.76.11', '218.92.227.170', '219.92.17.54', '221.214.166.181', '222.243.16.44'])
        anon_lvl = {'41.222.57.14': 'Transparent',
                    '54.207.45.19': 'Anonymous',
                    '60.191.174.13': 'Transparent',
                    '61.75.2.124': 'Transparent',
                    '69.168.242.44': 'High',
                    '69.168.255.85': 'High',
                    '94.23.200.49': 'Transparent',
                    '95.109.117.232': 'Transparent',
                    '118.144.156.2': 'Transparent',
                    '120.195.193.109': 'High',
                    '120.195.195.36': 'High',
                    '120.195.199.85': 'High',
                    '120.195.200.30': 'High',
                    '120.195.201.165': 'High',
                    '120.195.203.213': 'High',
                    '120.195.205.116': 'High',
                    '120.195.205.48': 'High',
                    '120.195.206.63': 'High',
                    '120.195.207.199': 'High',
                    '152.231.45.136': 'Transparent',
                    '152.26.69.30': 'Anonymous',
                    '152.26.69.31': 'Anonymous',
                    '152.26.69.32': 'Anonymous',
                    '152.26.69.33': 'Anonymous',
                    '152.26.69.36': 'Anonymous',
                    '152.26.69.39': 'Anonymous',
                    '152.26.69.40': 'Anonymous',
                    '152.26.69.41': 'Anonymous',
                    '152.26.69.42': 'Anonymous',
                    '152.26.69.45': 'Anonymous',
                    '187.18.122.211': 'Transparent',
                    '192.99.20.92': 'Anonymous',
                    '193.37.152.186': 'Transparent',
                    '210.51.48.206': 'Transparent',
                    '218.76.76.11': 'Transparent',
                    '218.92.227.170': 'Anonymous',  # OLD: 'High' but in headers have Proxy-Connection: close
                    '219.92.17.54': 'Transparent',
                    '222.243.16.44': 'Transparent',
                    '60.191.168.43': 'Transparent',
                    '62.204.241.146': 'Transparent',
                    '83.241.46.175': 'Transparent',
                    '212.119.242.68': 'Transparent',
                    '221.214.166.181': 'Transparent',
                    '190.15.192.120': 'Transparent',
                    '213.184.225.225': 'Transparent',
                    }

        goodProxies = sorted(goodProxies, key=lambda p: (len(p.host[:p.host.find('.')]), p.host[:3]))
        new = defaultdict(list)
        notFound = defaultdict(list)
        badAnon = []
        _all_good_hosts = []
        for p in goodProxies:
            _all_good_hosts.append(p.host)
            for pt in p.ptype:
                if pt == 'HTTP':
                    aLVL = anon_lvl.get(p.host)
                    if aLVL != p.anonymity['HTTP']:
                        badAnon.append('%s: OLD: %s; NOW: %s' % (p.host, aLVL, p.anonymity['HTTP']))
                if p.host in d[pt]:
                    continue
                elif p.host not in d[pt]:
                    new[pt].append(p)
        for pt, hosts in d.items():
            for host in hosts:
                if host not in _all_good_hosts:
                    notFound[pt].append(host)

        print('NEW:::')
        pprint.pprint(new)
        print('\nNOT FOUND:::')
        pprint.pprint(notFound)
        print('\nBAD ANON LVL:::')
        pprint.pprint(badAnon)

    # urllib.request.urlcleanup()

    testProxyList = [p.lstrip().split(':') for p in testProxyList.split('\n')
                     if p and not p.startswith('#')]

    # dest = {'host': 'fulltext.library.karelia.ru',
    #                 'port': 80,
    #                 'page': '/cgi-bin/env.pl',
    #                 'length': -1,
    #                 'search': ''}
    judges = ['http://jmof.org/proxyc/engine.php',
              'http://srv.com.eg/proxyc/engine.php',
              'http://www.pechati-don.ru/proxyc/engine.php',
              'http://fitnesser.ru/proxyc/engine.php',
              'http://kiev-hosting.com/prox33/engine.php',
              'http://ftp.cpdis.ru/engine.php',
              'http://shoppingtut.com/proxyc/engine.php',
              'http://web-sit.ru/prox/engine.php',
              'http://xzzy.info/ca/da/erpg.php',
              'http://xrumer.eu/admin/js/temptxt.php',
              'http://www.urlskit.com/herfer/engine.php',
              'http://plgetae.altervista.org/proxyc/engine.php',
              'http://noclo.com/engine.php',
              'http://www.leotraff.com/pp2p/engine.php',
              'http://sickseo.co.uk/proxyc/engine.php',
              'http://ns2.studio-aa.ru/engine.php',
              'http://pharmtex.com/proxyc/engine.php']
    s = ProxyChecker(proxies=testProxyList, judges=judges, showstats=True)
    s.start()

    psocks5 = sorted(s.get_good_proxies('SOCKS5'), key=lambda p: (len(p.host[:p.host.find('.')]), p.host[:3]))
    psocks4 = sorted(s.get_good_proxies('SOCKS4'), key=lambda p: (len(p.host[:p.host.find('.')]), p.host[:3]))
    phttps = sorted(s.get_good_proxies('HTTPS'), key=lambda p: (len(p.host[:p.host.find('.')]), p.host[:3]))
    phttp = sorted(s.get_good_proxies('HTTP'), key=lambda p: (len(p.host[:p.host.find('.')]), p.host[:3]))
    print('The amount of good proxies: %d\n' % len(s.get_good_proxies()))
    print('SOCKS5 (count: %d): %s\n' % (len(psocks5), psocks5))
    print('SOCKS4 (count: %d): %s\n' % (len(psocks4), psocks4))
    print('HTTPS (count: %d): %s\n' % (len(phttps), phttps))
    print('HTTP (count: %d): %s\n' % (len(phttp), phttp))

    test(s.get_good_proxies())


if __name__ == '__main__':
    main()

    # www.programcreek.com/python/example/1704/socket.inet_aton


# Works:
# - проверка прокси на работу с протоколами: HTTP, HTTPS (CONNECT), SOCKS4, SOCKS5
# - проверка уровня анонимности прокси
# - удаление дубликатов
# - получение рабочих прокси конкретного типа


# TODO:
# - поддержка авторизации (ip:port:login:pass)
# - проверка пинга
# - возможность указать адрес прокси без порта (пытаться соединиться по дфеолтовым портам)
# - возможность сохранять в отдельный текстовый/json/xml



# Чекер выполняет запросы через прокси, которые необходимо проверить, к судьям прокси
# с использованием всех возможных протоколов (HTTP, HTTPS, SOCKS4, SOCKS5) и с заданным
# пользователем таймаутом. Таким образом определяются поддерживаемые прокси сервером
# протоколы, а анализ HTTP заголовков соединения, полученных из ответа судьи прокси,
# позволяет определить уровень анонимности проверяемого прокси.