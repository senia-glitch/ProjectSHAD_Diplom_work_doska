from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from core import run

from app.access import require
from app.scenarios.boards import EmptyDTO

router = APIRouter(tags=['service'])


@router.get('/health', dependencies=[Depends(require('service.health'))])
def health():
    return {'status': 'ok'}


@router.get('/ready', dependencies=[Depends(require('service.ready'))])
async def ready():
    """HTTP → core → PostgreSQL. 503, если БД недоступна."""
    try:
        return (await run('service.ready', EmptyDTO())).model_dump()
    except Exception:
        return JSONResponse({'status': 'unavailable'}, status_code=503)
