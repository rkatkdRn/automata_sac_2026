"""FastAPI routes. No in-process game state is authoritative."""
from __future__ import annotations
import asyncio
import csv
import io
import json
import os
import time
import zipfile
from pathlib import Path
from urllib.parse import urlsplit
from fastapi import FastAPI, Body, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response, StreamingResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from . import db, service as s

PUBLIC = Path(__file__).resolve().parent.parent/'public'


def create_app():
    app=FastAPI(title='Colonel Blotto',docs_url=None,redoc_url=None,openapi_url=None)

    @app.middleware('http')
    async def protections(request: Request, call_next):
        if request.url.path.startswith('/api'):
            length=request.headers.get('content-length','0')
            if not length.isdigit() or int(length)>1200000:
                return JSONResponse({'error':{'code':'REQUEST_TOO_LARGE','message':'요청 파일이 너무 커요.'}},status_code=413)
            if request.method not in ('GET','HEAD','OPTIONS'):
                origin=request.headers.get('origin')
                configured=os.getenv('PUBLIC_BASE_URL','').rstrip('/')
                allowed={str(request.base_url).rstrip('/')}
                if configured: allowed.add(configured)
                if request.headers.get('sec-fetch-site')=='cross-site' or (origin is not None and origin.rstrip('/') not in allowed):
                    return JSONResponse({'error':{'code':'FORBIDDEN','message':'같은 사이트에서 다시 시도해 주세요.'}},status_code=403)
            try:
                response=await call_next(request)
            except RuntimeError:
                return JSONResponse({'error':{'code':'SETUP_REQUIRED','message':'서버의 데이터베이스 설정을 확인해 주세요.'}},status_code=503)
        else:
            response=await call_next(request)
        response.headers['X-Content-Type-Options']='nosniff'
        response.headers['Referrer-Policy']='strict-origin-when-cross-origin'
        response.headers['Cross-Origin-Opener-Policy']='same-origin-allow-popups'
        response.headers['X-Frame-Options']='DENY'
        if request.url.path.startswith('/api') or request.url.path=='/healthz': response.headers['Cache-Control']='no-store'
        return response

    @app.exception_handler(s.GameError)
    async def game_error(request,exc):
        return JSONResponse({'error':{'code':exc.code,'message':exc.message}},status_code=exc.status)

    @app.exception_handler(RequestValidationError)
    async def input_error(request,exc):
        # Never echo request input: validation failures can contain student names.
        return JSONResponse({'error':{'code':'INVALID_INPUT','message':'입력 형식을 확인해 주세요.'}},status_code=400)

    @app.exception_handler(Exception)
    async def internal_error(request,exc):
        # Do not include database DSNs, query parameters, names, or request bodies.
        return JSONResponse({'error':{'code':'SERVER_ERROR','message':'서버 연결에 문제가 있어요. 잠시 후 같은 요청으로 다시 시도해 주세요.'}},status_code=500)

    def player(request): return request.cookies.get(s.COOKIE)
    def admin(request): return request.cookies.get(s.ADMIN_COOKIE)
    def limited(request):
        token=player(request)
        if token:s.rate_limit('player:'+s.digest(token),10,10)
        return token
    def cookie_response(request, data, token, name, max_age):
        response=JSONResponse(data)
        response.set_cookie(name,token,httponly=True,secure=bool(os.getenv('VERCEL')) or request.url.scheme=='https',samesite='lax',max_age=max_age,path='/')
        return response

    @app.get('/healthz')
    def health():
        try:
            with db.transaction() as c:c.scalar('SELECT 1')
            return {'ok':True,'db':'ok','time':s.stamp()}
        except Exception:
            return JSONResponse({'ok':False,'db':'unavailable','time':s.stamp()},status_code=503)

    @app.post('/api/session')
    def login(request:Request,payload:dict=Body(...)):
        ip=request.client.host if request.client else 'unknown'
        s.rate_limit('login:'+s.digest(ip),60,60)
        data,token=s.login(payload,player(request))
        max_age=s.STUDENT_SESSION_DAYS*24*3600 if data['kind']=='STUDENT' else s.GUEST_SESSION_HOURS*3600
        return cookie_response(request,data,token,s.COOKIE,max_age)

    @app.post('/api/session/logout')
    def logout(request:Request):
        data=s.logout(player(request))
        response=JSONResponse(data);response.delete_cookie(s.COOKIE,path='/');return response

    @app.post('/api/me/nickname')
    def set_nickname(request:Request,payload:dict=Body(...)):return s.set_nickname(limited(request),payload)

    @app.get('/api/state')
    def state(request:Request):return s.state(player(request))

    @app.post('/api/submissions')
    def submit(request:Request,payload:dict=Body(...)):return s.submit(limited(request),payload)

    @app.get('/api/me/submissions')
    def history(request:Request):return s.history(player(request))

    @app.post('/api/entitlements/{entitlement_id}/choice')
    def select_choice(entitlement_id:str,request:Request,payload:dict=Body(...)):return s.select_choice(limited(request),entitlement_id,payload)

    @app.post('/api/entitlements/{entitlement_id}/claim')
    def claim(entitlement_id:str,request:Request,payload:dict=Body(...)):return s.claim(limited(request),entitlement_id,payload)

    @app.get('/api/scoreboard')
    def scoreboard():return s.board()

    @app.get('/api/hall')
    def hall(scope:str='DAY1'):return s.hall(scope)

    @app.get('/api/events')
    async def events(request:Request):
        token=player(request)
        def version():
            with db.transaction() as c:return int(db.config(c,'version','0'))
        async def stream():
            last_version=-1;last_state=None;start=time.monotonic()
            yield 'retry: 2000\n\n'
            while time.monotonic()-start<25:
                if await request.is_disconnected(): break
                current=await asyncio.to_thread(version)
                state=await asyncio.to_thread(s.state,token)
                signature={k:v for k,v in state.items() if k!='server_time'}
                if signature!=last_state:
                    yield 'event: state.changed\ndata: '+s.dumps(state)+'\n\n';last_state=signature
                if current!=last_version:
                    board=await asyncio.to_thread(s.board)
                    yield 'event: board.updated\ndata: '+s.dumps(board)+'\n\n';last_version=current
                yield 'event: heartbeat\ndata: {}\n\n'
                await asyncio.sleep(1.5)
        # Force configuration/auth-independent DB validation before response headers.
        await asyncio.to_thread(version)
        return StreamingResponse(stream(),media_type='text/event-stream',headers={'Cache-Control':'no-store','X-Accel-Buffering':'no','Connection':'keep-alive'})

    @app.post('/api/admin/login')
    def admin_login(request:Request,payload:dict=Body(...)):
        data,token=s.admin_login(payload.get('pin'),request.client.host if request.client else 'unknown')
        return cookie_response(request,data,token,s.ADMIN_COOKIE,12*3600)

    @app.post('/api/admin/logout')
    def admin_logout(request:Request):
        data=s.logout(admin(request));response=JSONResponse(data);response.delete_cookie(s.ADMIN_COOKIE,path='/');return response

    @app.get('/api/admin/dashboard')
    def dashboard(request:Request):return s.dashboard(admin(request))
    @app.post('/api/admin/day')
    def choose_day(request:Request,payload:dict=Body(...)):return s.choose_day(admin(request),payload)
    @app.post('/api/admin/day-config')
    def configure_day(request:Request,payload:dict=Body(...)):return s.configure_day(admin(request),payload)
    @app.post('/api/admin/booth')
    def booth(request:Request,payload:dict=Body(...)):return s.booth(admin(request),payload)
    @app.get('/api/admin/threshold/suggest')
    def suggest(request:Request):return s.suggest(admin(request))
    @app.post('/api/admin/threshold')
    def set_threshold(request:Request,payload:dict=Body(...)):return s.set_threshold(admin(request),payload)
    @app.get('/api/admin/seeds')
    @app.get('/api/admin/seeds/export')
    def seeds(request:Request):return s.seeds(admin(request))
    @app.post('/api/admin/seeds')
    def add_seed(request:Request,payload:dict=Body(...)):return s.add_seeds(admin(request),payload,True)
    @app.post('/api/admin/seeds/import')
    def import_seeds(request:Request,payload:dict=Body(...)):return s.add_seeds(admin(request),payload)
    @app.get('/api/admin/claims')
    def claims(request:Request,status:str|None=None):return s.claims(admin(request),status)
    @app.get('/api/admin/inventory')
    def stock(request:Request):return s.stock(admin(request))
    @app.post('/api/admin/inventory/count')
    def stock_count(request:Request,payload:dict=Body(...)):return s.stock_count(admin(request),payload)
    @app.post('/api/admin/entitlements/{entitlement_id}/void')
    def void(entitlement_id:str,request:Request,payload:dict=Body(...)):return s.void_entitlement(admin(request),entitlement_id,payload)
    @app.get('/api/admin/submissions')
    def submission_log(request:Request,day:str|None=None):return s.submission_log(admin(request),day)
    @app.post('/api/admin/submissions/{submission_id}/snack')
    def adjust_snack(submission_id:str,request:Request,payload:dict=Body(...)):return s.adjust_snack(admin(request),submission_id,payload)
    @app.post('/api/admin/submissions/{submission_id}/delete')
    def delete_submission(submission_id:str,request:Request,payload:dict=Body(...)):return s.delete_submission(admin(request),submission_id,payload)
    @app.get('/api/admin/participants')
    def people(request:Request):return s.participants(admin(request))
    @app.post('/api/admin/participants/{participant_id}/hide')
    def hide(participant_id:str,request:Request,payload:dict=Body(...)):return s.hide(admin(request),participant_id,payload)
    @app.post('/api/admin/freeze')
    def freeze(request:Request,payload:dict=Body(...)):return s.freeze(admin(request),payload)
    @app.get('/api/admin/audit')
    def audit(request:Request):return s.audits(admin(request))
    @app.post('/api/admin/purge')
    def purge(request:Request,payload:dict=Body(...)):
        response=JSONResponse(s.purge(admin(request),payload));response.delete_cookie(s.COOKIE,path='/');response.delete_cookie(s.ADMIN_COOKIE,path='/');return response
    @app.post('/api/admin/reset')
    def reset(request:Request,payload:dict=Body(...)):return s.reset(admin(request),payload)
    @app.post('/api/admin/test/clear')
    def clear_test(request:Request):return s.clear_test(admin(request))

    @app.get('/api/admin/backup')
    def backup(request:Request):
        data=s.export_data(admin(request))
        return Response(s.dumps(data),media_type='application/json',headers={'Content-Disposition':'attachment; filename="blotto-anonymous-export.json"'})

    @app.get('/api/admin/export')
    def export(request:Request):
        data=s.export_data(admin(request));buffer=io.BytesIO()
        with zipfile.ZipFile(buffer,'w',zipfile.ZIP_DEFLATED) as archive:
            for name,rows in data['tables'].items():
                output=io.StringIO(newline='')
                if rows:
                    writer=csv.DictWriter(output,fieldnames=list(rows[0]));writer.writeheader();writer.writerows(rows)
                archive.writestr(name+'.csv','\ufeff'+output.getvalue())
        return Response(buffer.getvalue(),media_type='application/zip',headers={'Content-Disposition':'attachment; filename="blotto-records.zip"'})

    @app.get('/api/admin/qr')
    def qr(request:Request):
        with db.transaction() as c:s.require_admin(c,admin(request))
        import qrcode
        import qrcode.image.svg
        url=os.getenv('PUBLIC_BASE_URL',str(request.base_url)).rstrip('/')+'/'
        image=qrcode.make(url,image_factory=qrcode.image.svg.SvgPathImage,box_size=10,border=4)
        output=io.BytesIO();image.save(output)
        return Response(output.getvalue(),media_type='image/svg+xml')

    if not os.getenv('VERCEL'):
        @app.get('/')
        @app.get('/play')
        @app.get('/result')
        @app.get('/closed')
        @app.get('/board')
        @app.get('/hall')
        @app.get('/admin')
        @app.get('/privacy')
        def frontend():return FileResponse(PUBLIC/'index.html')
        app.mount('/',StaticFiles(directory=str(PUBLIC),check_dir=False),name='public')
    return app
