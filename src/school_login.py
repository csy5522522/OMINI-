"""Direct Huali WeChat/CAS login with per-user, in-memory school sessions."""
from dataclasses import dataclass, field
import base64
import html
import re
import secrets
import threading
import time
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit, urlunsplit

import requests

import school_timetable as school

SSO = 'https://sso.hualixy.edu.cn/heer_cas/api/heer_cas/login'
SERVICE = school.BASE_URL + '/sso/ddlogin'
AUTH_HOSTS = {'sso.hualixy.edu.cn', school.HOST}
WX_HOSTS = {'open.weixin.qq.com', 'lp.open.weixin.qq.com', 'long.open.weixin.qq.com'}


def checked_url(url, hosts):
    parsed = urlsplit(url)
    if parsed.scheme != 'https' or parsed.hostname not in hosts or parsed.port not in (None, 443) or parsed.username or parsed.password:
        raise ValueError('学校登录返回了非预期的跳转地址，请重新登录。')
    return url


def school_redirect(url):
    """The school's reverse proxy emits HTTP Locations; keep the transport HTTPS."""
    parsed = urlsplit(url)
    if (parsed.scheme == 'http' and parsed.hostname in AUTH_HOSTS and parsed.port in (None, 80)
            and not parsed.username and not parsed.password):
        url = urlunsplit(('https', parsed.hostname, parsed.path, parsed.query, parsed.fragment))
    return checked_url(url, AUTH_HOSTS)


@dataclass
class LoginFlow:
    owner: tuple
    client: object
    year: str
    term: str
    revision: int
    created: float
    uuid: str = ''
    callback: str = ''
    last: int = 408
    status: str = 'waiting'
    cancelled: bool = False
    result: dict | None = None
    identity: str = ''
    lock: object = field(default_factory=threading.Lock)


class SchoolLoginService:
    def __init__(self, session_factory=requests.Session, clock=time.monotonic):
        self.flows = {}
        self.lock = threading.RLock()
        self.session_factory, self.clock = session_factory, clock

    def _cleanup(self):
        for key, flow in list(self.flows.items()):
            ttl = 7200 if flow.status == 'complete' else 300
            if flow.cancelled or self.clock() - flow.created > ttl:
                flow.cancelled = True
                self.flows.pop(key, None)
                flow.client.close()

    @staticmethod
    def _request(flow, method, url, *, hosts=AUTH_HOSTS, limit=4*1024*1024, **kwargs):
        checked_url(url, hosts)
        try:
            response = flow.client.request(method, url, timeout=(5, 12), allow_redirects=False, stream=True, **kwargs)
            data = bytearray()
            try:
                for chunk in response.iter_content(65536):
                    data.extend(chunk)
                    if len(data) > limit:
                        raise ValueError('学校登录响应过大，请重试。')
            finally:
                response.close()
            if response.status_code >= 400:
                raise ValueError('学校登录服务暂时不可用，请稍后重试。')
            return response, bytes(data)
        except requests.Timeout:
            raise
        except requests.RequestException:
            raise ValueError('连接学校登录服务失败，请检查网络后重试。') from None

    def _follow(self, flow, url):
        for _ in range(10):
            response, raw = self._request(flow, 'GET', url)
            if response.status_code not in (301, 302, 303, 307, 308):
                return response, raw
            url = school_redirect(urljoin(url, response.headers.get('Location', '')))
            target = urlsplit(url)
            # CAS returns to its Vue login route. The browser's next action is the
            # clients API call, which poll() makes explicitly with this cookie jar.
            if target.hostname == 'sso.hualixy.edu.cn' and target.path.rstrip('/') == '/heer_cas' and target.fragment.startswith('/login'):
                return response, raw
        raise ValueError('学校登录跳转次数过多，请重新扫码。')

    @staticmethod
    def _json(raw):
        import json
        try:
            return json.loads(raw.decode('utf-8-sig'))
        except (UnicodeError, ValueError):
            raise ValueError('学校登录会话已过期，请重新扫码。') from None

    def start(self, owner, year, term, revision):
        year, term = str(year), str(term)
        if not re.fullmatch(r'\d{4}', year) or term not in school.TERMS or type(revision) is not int:
            raise ValueError('请选择有效的学年和学期。')
        with self.lock:
            self._cleanup()
            self.clear_owner(owner)
            if sum(flow.status != 'complete' for flow in self.flows.values()) >= 64:
                raise ValueError('学校登录人数较多，请稍后再试。')
            while len(self.flows)>=256:
                key,old=min(((key,flow) for key,flow in self.flows.items() if flow.status=='complete'),key=lambda item:item[1].created)
                self.cancel(key,old.owner)
            client = self.session_factory(); client.trust_env = False
            client.headers.update({'User-Agent': 'Mozilla/5.0 OminiTimetable/1.0'})
            flow = LoginFlow(owner, client, year, term, revision, self.clock())
            key = secrets.token_urlsafe(24); self.flows[key] = flow
        try:
            _, raw = self._request(flow, 'GET', SSO, params={'type': 'clients', 'service': SERVICE})
            value = self._json(raw)
            link = value.get('data', {}).get('wx')
            if not isinstance(link, str):
                raise ValueError('学校暂未返回微信登录入口，请稍后重试。')
            checked_url(link, {'open.weixin.qq.com'})
            query = parse_qs(urlsplit(link).query)
            flow.callback = checked_url(query.get('redirect_uri', [''])[0], {'sso.hualixy.edu.cn'})
            callback = urlsplit(flow.callback)
            if callback.path != '/heer_cas/api/heer_cas/login' or parse_qs(callback.query).get('service') != [SERVICE]:
                raise ValueError('学校认证入口与教务系统不匹配，请重试。')
            _, raw = self._request(flow, 'GET', link, hosts={'open.weixin.qq.com'})
            match = re.search(r'/connect/qrcode/([A-Za-z0-9_-]{8,128})', html.unescape(raw.decode('utf-8', errors='replace')))
            if not match:
                raise ValueError('学校微信二维码暂时不可用，请点击重新生成。')
            flow.uuid = match[1]
            _, image = self._request(flow, 'GET', 'https://open.weixin.qq.com/connect/qrcode/' + flow.uuid, hosts={'open.weixin.qq.com'}, limit=1024*1024)
            mime = 'image/png' if image.startswith(b'\x89PNG\r\n\x1a\n') else 'image/jpeg' if image.startswith(b'\xff\xd8\xff') else ''
            if not mime:
                raise ValueError('二维码图片读取失败，请重试。')
            if flow.cancelled:
                raise ValueError('学校登录已取消。')
            return {'id': key, 'status': 'waiting', 'qr_image': 'data:' + mime + ';base64,' + base64.b64encode(image).decode(), 'expires_in': 300}
        except Exception as error:
            self.cancel(key, owner)
            if isinstance(error, requests.Timeout):
                raise ValueError('学校登录服务响应超时，请重试。') from None
            raise

    def _get(self, key, owner):
        with self.lock:
            self._cleanup()
            flow = self.flows.get(key)
            if flow is None or flow.owner != owner or flow.cancelled:
                raise ValueError('学校登录已取消或二维码已过期，请重新生成。')
            return flow

    def _fetch_schedule(self, flow, year, term):
        def post(path, values):
            response, raw = self._request(flow, 'POST', school.BASE_URL + path + '?gnmkdm=N2151', data=values,
                headers={'Referer': school.PAGE_URL, 'Origin': school.BASE_URL, 'X-Requested-With': 'XMLHttpRequest'})
            if response.status_code in (301, 302, 303, 307, 308):
                raise ValueError('学校登录会话已过期，请重新扫码。')
            return self._json(raw)
        payload = post(school.COURSE_PATH, {'xnm': year, 'xqm': term, 'kzlx': 'ck', 'xsdm': '', 'kclbdm': '', 'kclxdm': ''})
        result = school.parse_schedule(payload, year, term)
        if not flow.identity and isinstance(payload.get('xsxx'),dict):
            identity = payload['xsxx'].get('XH') or payload['xsxx'].get('xh')
            if isinstance(identity,str) and 0<len(identity.strip())<=200: flow.identity=identity.strip()
        try:
            campus = str(payload['kbList'][0].get('xqh_id', '1')) if payload['kbList'] else '1'
            periods = post(school.PERIOD_PATH, {'xnm': year, 'xqm': term, 'xqh_id': campus})
            if isinstance(periods, list): result = school.parse_schedule(payload, year, term, periods)
        except (ValueError, requests.Timeout):
            result['warning'] = '课表已导入，节次时间暂未读取。'
        return result

    def poll(self, key, owner, save):
        flow = self._get(key, owner)
        if not flow.lock.acquire(blocking=False): return {'status': 'waiting'}
        try:
            if flow.result is not None: return {'status': 'complete', 'result': flow.result}
            try:
                _, raw = self._request(flow, 'GET', 'https://lp.open.weixin.qq.com/connect/l/qrconnect', hosts=WX_HOSTS,
                    params={'uuid': flow.uuid, 'last': str(flow.last)}, limit=16384)
            except requests.Timeout:
                return {'status': 'scanned' if flow.last == 404 else 'waiting'}
            text = raw.decode('utf-8', errors='replace')
            match = re.search(r'wx_errcode\s*=\s*(\d+)', text)
            code = int(match[1]) if match else 408
            if code in (402, 403):
                self.cancel(key, owner); return {'status': 'expired'}
            if code != 405:
                flow.last = code
                return {'status': 'scanned' if code == 404 else 'waiting'}
            match = re.search(r'wx_code\s*=\s*["\']([A-Za-z0-9_-]{1,256})["\']', text)
            if not match: raise ValueError('学校扫码确认结果有误，请重新扫码。')
            separator = '&' if '?' in flow.callback else '?'
            self._follow(flow, flow.callback + separator + urlencode({'code': match[1], 'state': ''}))
            _, raw = self._request(flow, 'GET', SSO, params={'type': 'clients', 'service': SERVICE})
            account = self._json(raw).get('data', {})
            identity = account.get('username')
            if isinstance(identity,str) and 0<len(identity.strip())<=200: flow.identity=identity.strip()
            redirect = account.get('redirect')
            if not isinstance(redirect, str):
                raise ValueError('学校微信账号尚未完成教务认证，请先在学校统一认证中绑定账号后重试。')
            target = school_redirect(redirect)
            checked_url(target, {school.HOST})
            self._follow(flow, target)
            schedule = self._fetch_schedule(flow, flow.year, flow.term)
            if flow.cancelled: raise ValueError('学校登录已取消。')
            flow.result = save(schedule, flow.revision)
            flow.status = 'complete'; flow.created = self.clock()
            return {'status': 'complete', 'result': flow.result}
        except requests.Timeout:
            raise ValueError('学校认证或课表读取超时，请重新扫码。') from None
        finally:
            flow.lock.release()

    def connected(self, owner):
        with self.lock:
            self._cleanup()
            return any(flow.owner == owner and flow.status == 'complete' for flow in self.flows.values())

    def verified_identity(self, key, owner):
        identity=self._get(key,owner).identity
        if not identity: raise ValueError('学校未返回可验证的账号身份，请重新扫码。')
        return identity

    def adopt(self,key,owner,new_owner):
        with self.lock:
            flow=self._get(key,owner)
            if flow.status!='complete': raise ValueError('学校登录尚未完成。')
            flow.owner=new_owner

    def refresh(self, owner, year, term):
        year, term = str(year), str(term)
        if not re.fullmatch(r'\d{4}', year) or term not in school.TERMS:
            raise ValueError('请选择有效的学年和学期。')
        with self.lock:
            self._cleanup()
            flow = next((f for f in self.flows.values() if f.owner == owner and f.status == 'complete'), None)
        if flow is None: raise ValueError('请先微信扫码登录学校，连接后即可在线更新。')
        with flow.lock:
            try: return self._fetch_schedule(flow, year, term)
            except requests.Timeout: raise ValueError('课表读取超时，请稍后重试。') from None

    def cancel(self, key, owner, keep_complete=False):
        with self.lock:
            flow = self.flows.get(key)
            if flow and flow.owner == owner and not (keep_complete and flow.status == 'complete'):
                flow.cancelled = True; self.flows.pop(key, None); flow.client.close()

    def clear_owner(self, owner):
        with self.lock:
            for key, flow in list(self.flows.items()):
                if flow.owner == owner: self.cancel(key, owner)
