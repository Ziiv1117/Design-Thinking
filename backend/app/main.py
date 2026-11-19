"""FastAPI application for notice intake, analysis, and task review."""
import os
import math
import asyncio
from contextlib import asynccontextmanager
import anyio
from uuid import uuid4
from pathlib import Path
from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from sqlalchemy import text
from sqlalchemy.exc import OperationalError
from app.core.current_user import configure_demo_user
from app.core.database import create_database
from app.core.errors import BusinessError
from app.routers import health, imports, review, tasks
from app.services.analysis_service import AnalysisService, recover_interrupted


def _timeout_setting(name, default):
    try:
        value = float(os.getenv(name, str(default)))
    except ValueError as exc:
        raise RuntimeError(f'{name} must be a positive number no greater than {default}') from exc
    if not math.isfinite(value) or not 0 < value <= default:
        raise RuntimeError(f'{name} must be a positive number no greater than {default}')
    return value


def create_app(engine=None, user_id=None, *, agent=None, ocr=None):
    @asynccontextmanager
    async def lifespan(app):
        owned = engine is None
        analysis_timeout = _timeout_setting('ANALYSIS_TIMEOUT_SECONDS', 90)
        ocr_timeout = _timeout_setting('OCR_TIMEOUT_SECONDS', 30)
        try:
            if owned:
                app.state.engine = create_database(os.getenv('DATABASE_URL','sqlite:///./data/deadline_keeper.db'))
                with app.state.engine.connect() as connection:
                    try:
                        revision = connection.execute(text('SELECT version_num FROM alembic_version')).scalar_one()
                    except Exception as error:
                        raise RuntimeError('Database migration required: run alembic upgrade head') from error
                    if revision != '0001_initial':
                        raise RuntimeError('Database migration is not at the expected revision')
                app.state.user_id = configure_demo_user(app.state.engine)
                from app.adapters.agent_adapter import AgentAdapter
                from app.adapters.ocr_adapter import OCRAdapter
                app.state.agent = AgentAdapter()
                app.state.ocr = await anyio.to_thread.run_sync(OCRAdapter, os.getenv('OCR_MODEL_DIR','./models/paddleocr'))
            app.state.analysis = AnalysisService(app.state.engine, app.state.agent, timeout=analysis_timeout)
            app.state.ocr_slot = asyncio.Semaphore(1)
            app.state.ocr_timeout = ocr_timeout
            await anyio.to_thread.run_sync(recover_interrupted,app.state.engine)
            yield
        finally:
            if owned:
                if app.state.agent is not None:
                    await app.state.agent.aclose()
                if getattr(app.state, 'engine', None) is not None:
                    app.state.engine.dispose()
    app = FastAPI(title='Deadline Keeper', lifespan=lifespan)
    app.state.agent, app.state.ocr = agent, ocr
    app.state.ocr_timeout = 30
    app.state.ocr_slot = asyncio.Semaphore(1)
    if engine is not None:
        app.state.engine, app.state.user_id = engine, user_id

    @app.middleware('http')
    async def request_identifier(request, call_next):
        request.state.request_id = f'req_{uuid4().hex}'
        response = await call_next(request)
        response.headers['X-Request-Id'] = request.state.request_id
        return response

    def error_response(request, code, message, status, details=None):
        return JSONResponse(status_code=status, headers={'X-Request-Id':request.state.request_id}, content={'error':{'code':code,'message':message,'request_id':request.state.request_id,'details':details or {}}})

    @app.exception_handler(BusinessError)
    async def business_error(request, error):
        return error_response(request,error.code,error.message,error.status,error.details)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, error):
        if request.url.path == '/api/v1/imports/text' and any(e['type'] == 'string_too_long' and tuple(e['loc']) == ('body','text') for e in error.errors()):
            return error_response(request,'INPUT_TOO_LARGE','文字超过长度限制',413)
        fields = [{'field':'.'.join(map(str,e['loc'])), 'message':e['msg']} for e in error.errors()]
        return error_response(request,'VALIDATION_ERROR','请求字段不合法',422,{'fields':fields})

    @app.exception_handler(OperationalError)
    async def database_error(request, error):
        if 'locked' in str(error.orig).lower() or 'busy' in str(error.orig).lower():
            return error_response(request,'DATABASE_BUSY','数据库繁忙，请稍后重试',503)
        return error_response(request,'DEPENDENCY_NOT_READY','数据库不可用',503)

    @app.exception_handler(Exception)
    async def unexpected_error(request, error):
        return error_response(request,'INTERNAL_ERROR','服务内部错误',500)

    @app.get('/', include_in_schema=False)
    def test_page():
        return FileResponse(Path(__file__).parent / 'static' / 'index.html')

    app.include_router(health.router)
    app.include_router(imports.router)
    app.include_router(tasks.router)
    app.include_router(review.router)
    return app

app = create_app()
