"""Authenticated, read-only website queries in a cancellable HTTP worker."""
import json
import base64
import re
import tempfile
import threading
from PyQt5 import QtCore
from .rh_model_favorites import SITES, TYPES

_reads=threading.BoundedSemaphore(2)


def post_json(session, site, endpoint, body, stop=None):
    if site not in SITES or endpoint not in ('/uc/getUserInfo','/api/likeOrCollect/resource/list',
            '/api/resource/list','/api/resource/detail/get','/api/resource/baseModels'):
        raise ValueError('无效的读取请求')
    import requests
    for attempt in range(3):
        if stop and stop.is_set():raise InterruptedError()
        try:
            with session.post(site+endpoint,json=body,timeout=(5,15),stream=True,allow_redirects=False) as response:
                if response.status_code in (401,403):raise ValueError('登录已失效，请在默认浏览器登录当前站点后重新读取')
                response.raise_for_status();raw=bytearray()
                for chunk in response.iter_content(32768):
                    if stop and stop.is_set():raise InterruptedError()
                    raw.extend(chunk)
                    if len(raw)>4*1024*1024:raise ValueError('官网单页数据过大，已停止读取')
            try:payload=json.loads(raw)
            except (ValueError,UnicodeError):raise ValueError('官网返回了无法识别的数据，请稍后重试') from None
            if not isinstance(payload,dict) or str(payload.get('code'))!='0':
                if isinstance(payload,dict) and str(payload.get('code')) in ('401','403','412'):
                    raise ValueError('登录已失效，请在默认浏览器登录当前站点后重新读取')
                code=str(payload.get('code')) if isinstance(payload,dict) else ''
                suffix='，错误码 '+code if re.fullmatch(r'\d{1,8}',code) else ''
                if code=='301':raise ValueError('官网请求参数无效（'+endpoint+suffix+'），请更新客户端或反馈此错误')
                raise ValueError('官网读取失败（'+endpoint+suffix+'），请稍后重试')
            return payload.get('data')
        except requests.RequestException:
            if attempt==2:raise ValueError('网络读取失败，可导入已读取部分后重试') from None
            if stop:
                if stop.wait(.5*(attempt+1)):raise InterruptedError()


def website_user(session, site, stop=None):
    """Mirror the website: JWT sub supplies userId; the server verifies the token.

    Decoding a claim here is only request construction, not authentication.
    """
    authorization=session.headers.get('Authorization') or ''
    try:
        if not authorization.startswith('Bearer ') or len(authorization)>16000:raise ValueError()
        parts=authorization[7:].split('.')
        if len(parts)!=3:raise ValueError()
        raw=base64.b64decode(parts[1]+'='*(-len(parts[1])%4),altchars=b'-_',validate=True)
        subject=json.loads(raw).get('sub')
        if isinstance(subject,bool) or not isinstance(subject,(str,int)):raise ValueError()
        identity=str(subject)
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,128}',identity):raise ValueError()
    except (ValueError,TypeError,AttributeError,UnicodeError):
        raise ValueError('无法识别官网登录账号，请在默认浏览器重新登录后读取') from None
    user=post_json(session,site,'/uc/getUserInfo',{'userId':identity},stop)
    if not isinstance(user,dict) or str(user.get('id') or '')!=identity:
        raise ValueError('官网返回的账号与当前登录会话不一致，请在默认浏览器重新登录后读取')
    return user


def project_record(record):
    if not isinstance(record,dict):raise ValueError('官网返回了无效模型')
    fields=('id','resourceName','resourceType','nodeModelName','thumbnailUrl','posterUrl','desc','tags','versions')
    value={key:record.get(key) for key in fields}
    value['desc']=str(value['desc'] or '')[:8000]
    value['versions']=[{key:v.get(key) for key in ('id','version','versionResourceName','baseModel','triggerWords','desc','syncStatus','posterInfos')}
                       for v in (record.get('versions') or [])[:50] if isinstance(v,dict)]
    return value


class CollectionRead(QtCore.QObject):
    progress=QtCore.pyqtSignal(int,str)
    finished=QtCore.pyqtSignal(object)

    def __init__(self, site, bucket, authorization, parent=None):
        super().__init__(parent)
        if site not in SITES or bucket not in ('favorites','uploads'):raise ValueError('无效的读取目标')
        self.site,self.bucket=site,bucket;self.authorization=authorization
        self.stop=threading.Event();self.active=False;self.discard=False

    def start(self):
        if self.active:return
        self.active=True
        threading.Thread(target=self._work,name='rh-personal-models-http',daemon=True).start()

    def cancel(self):self.stop.set()

    def close(self):self.discard=True;self.cancel()

    def _work(self):
        import requests
        stage=None;count=0;error='';acquired=False
        auth=self.authorization;self.authorization=''
        try:
            acquired=_reads.acquire(blocking=False)
            if not acquired:raise ValueError('已有两个官网读取任务，请稍后重试')
            if not isinstance(auth,str) or not auth.startswith('Bearer ') or len(auth)>16000 or '\n' in auth or '\r' in auth:
                raise ValueError('请先在默认浏览器登录当前站点')
            stage=tempfile.TemporaryFile(mode='w+t',encoding='utf8')
            with requests.Session() as session:
                session.headers.update({'Authorization':auth,'Content-Type':'application/json','Origin':self.site,'Referer':self.site+'/'})
                auth=''
                try:
                    user=website_user(session,self.site,self.stop)
                    uid=str(user['id']);seen=set()
                    endpoint='/api/likeOrCollect/resource/list' if self.bucket=='favorites' else '/api/resource/list'
                    for kind in TYPES:
                        for current in range(1,1001):
                            body=dict(size=30,current=current,resourceType=kind,resourceName='',tags=None,baseModels=[],point='')
                            if self.bucket=='favorites':body['operateType']=2
                            else:body.update(systemResource=False,choiceModel=False)
                            data=post_json(session,self.site,endpoint,body,self.stop)
                            if not isinstance(data,dict) or not isinstance(data.get('records'),list) or len(data['records'])>30:
                                raise ValueError('官网分页数据异常，已停止读取')
                            if data.get('current') is not None and int(data['current'])!=current:raise ValueError('官网返回了错误分页')
                            rows=data['records'];added=0
                            for record in rows:
                                if self.stop.is_set():raise InterruptedError()
                                identity=str(record.get('id') or '')
                                if not identity:raise ValueError('模型缺少标识，已停止读取')
                                if identity in seen:continue
                                if record.get('resourceType')!=kind:raise ValueError('官网返回了其他类别的模型，已停止读取')
                                if self.bucket=='uploads':
                                    owner=record.get('owner') or {}
                                    if isinstance(owner,dict) and owner.get('id') is not None and str(owner['id'])!=uid:
                                        raise ValueError('官网返回的模型不属于当前账号，已停止读取')
                                if count>=10000:raise ValueError('已达到单次 10000 个模型上限，可先导入已读取部分')
                                seen.add(identity);stage.write(json.dumps(project_record(record),ensure_ascii=False)+'\n');count+=1;added+=1
                            self.progress.emit(count,kind)
                            total=data.get('total');more=data.get('hasNext') is True or (isinstance(total,(int,float)) and total>current*30)
                            if not rows or data.get('hasNext') is False or (len(rows)<30 and not more):break
                            if not added:raise ValueError('官网返回重复分页，已停止读取')
                            if current==1000:raise ValueError('达到分页上限，可先导入已读取部分')
                            if self.stop.wait(.25):raise InterruptedError()
                finally:session.headers.clear()
        except InterruptedError:error='读取已停止，可导入已读取部分。'
        except ValueError as exc:error=str(exc)
        except Exception:error='读取中断，可导入已读取部分后重试。'
        finally:
            auth='';self.active=False
            if acquired:_reads.release()
        if stage:stage.seek(0)
        if self.discard:
            if stage:stage.close()
            return
        try:self.finished.emit(dict(stage=stage,count=count,error=error,site=self.site,bucket=self.bucket))
        except RuntimeError:
            if stage:stage.close()
