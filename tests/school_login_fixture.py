"""Synthetic upstreams modeled on the supplied school login request sequence."""
import base64
import json
from urllib.parse import parse_qs, urlencode, urlsplit

from school_login import SSO, SERVICE
import school_timetable as school

PNG = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/l2cAAAAASUVORK5CYII=')
PAYLOAD = {'xsxx': {'XH': 'PRIVATE_STUDENT'}, 'kbList': [{'kcmc': '直接抓取课表', 'xqj': '1', 'jcs': '1-2', 'zcd': '1-16周', 'xm': '教师', 'cdmc': 'A201'}]}


class Response:
    def __init__(self, value=b'', status=200, headers=None):
        self.status_code, self.headers = status, headers or {}
        self.raw = value if isinstance(value, bytes) else json.dumps(value).encode()

    def iter_content(self, _):
        yield self.raw

    def close(self):
        pass


class FakeSchoolSession:
    def __init__(self):
        self.headers = {};self.authenticated = False;self.school_ready = False
        self.codes = [408, 404, 405];self.calls = [];self.closed = False
        self.username = 'PRIVATE_STUDENT'
        self.block_course = None

    def close(self):
        self.closed = True

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        parsed = urlsplit(url)
        if url == SSO and kwargs.get('params', {}).get('type') == 'clients':
            if self.authenticated:
                return Response({'code': 0, 'data': {'redirect': SERVICE+'?ticket=PRIVATE_TICKET','username':self.username}})
            callback = SSO + '?' + urlencode({'client_name':'WeixinClient','service':SERVICE})
            return Response({'code':0,'data':{'wx':'https://open.weixin.qq.com/connect/qrconnect?'+urlencode({'appid':'FIXTURE','redirect_uri':callback,'response_type':'code','scope':'snsapi_login'})}})
        if parsed.hostname == 'open.weixin.qq.com' and parsed.path == '/connect/qrconnect':
            return Response(b'<img src="/connect/qrcode/FIXTURE_UUID_123">')
        if parsed.hostname == 'open.weixin.qq.com' and parsed.path.startswith('/connect/qrcode/'):
            return Response(PNG)
        if parsed.path == '/connect/l/qrconnect':
            code = self.codes.pop(0) if len(self.codes) > 1 else self.codes[0]
            return Response(f'window.wx_errcode={code};window.wx_code=\'FIXTURE_CODE\';'.encode())
        if parsed.hostname == 'sso.hualixy.edu.cn' and 'code' in parse_qs(parsed.query):
            self.authenticated = True
            return Response(status=302, headers={'Location':'http://sso.hualixy.edu.cn/heer_cas/#/login?service=https%3A%2F%2Fjw.hualixy.edu.cn%2Fsso%2Fddlogin'})
        if parsed.hostname == 'sso.hualixy.edu.cn' and parsed.path == '/heer_cas/':
            return Response(b'<html>SSO</html>')
        if parsed.hostname == school.HOST and parsed.path == '/sso/ddlogin':
            return Response(status=302, headers={'Location':'https://jw.hualixy.edu.cn/jwglxt/ticketlogin?uid=PRIVATE_STUDENT&timestamp=FIXTURE&verify=PRIVATE_VERIFY'})
        if parsed.hostname == school.HOST and parsed.path == '/jwglxt/ticketlogin':
            self.school_ready = True
            return Response(status=302, headers={'Location':'http://jw.hualixy.edu.cn/jwglxt/xtgl/login_slogin.html'})
        if parsed.hostname == school.HOST and parsed.path == '/jwglxt/xtgl/login_slogin.html':
            return Response(status=302, headers={'Location':'http://jw.hualixy.edu.cn/jwglxt/xtgl/index_initMenu.html'})
        if parsed.hostname == school.HOST and parsed.path == '/jwglxt/xtgl/index_initMenu.html':
            return Response(b'<html>school</html>')
        if parsed.hostname == school.HOST and parsed.path == school.COURSE_PATH:
            assert self.school_ready
            if self.block_course: self.block_course()
            return Response(PAYLOAD)
        if parsed.hostname == school.HOST and parsed.path == school.PERIOD_PATH:
            assert self.school_ready
            return Response([{'jcmc':'1','qssj':'08:00','jssj':'08:45'}])
        raise AssertionError('Unexpected fixture request')
