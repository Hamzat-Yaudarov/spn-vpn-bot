"""Authenticated native referral routes. The app supplies Telegram authentication."""
import logging

from fastapi import APIRouter, HTTPException, Request

from services import miniapp_referral as actions, referral_accounting as accounting

logger = logging.getLogger(__name__)


def create_router(auth_user, bot_provider):
    router = APIRouter(prefix='/miniapp/api/referral')

    async def body(request):
        try:
            return await request.json()
        except (ValueError, UnicodeDecodeError) as exc:
            raise HTTPException(400, detail={'code': 'invalid_request', 'message': 'Некорректные данные запроса.'}) from exc

    async def run(operation):
        try:
            return await operation
        except actions.ActionError as exc:
            raise HTTPException(exc.status, detail=exc.detail) from exc
        except accounting.InsufficientBalance as exc:
            raise HTTPException(409, detail={'code': 'insufficient_balance', 'message': str(exc),
                'available': actions.amount_text(exc.available), 'amount': actions.amount_text(exc.amount)}) from exc
        except accounting.RequestConflict as exc:
            raise HTTPException(409, detail={'code': 'request_conflict', 'message': str(exc)}) from exc
        except ValueError as exc:
            raise HTTPException(400, detail={'code': 'invalid_request', 'message': str(exc)}) from exc
        except Exception as exc:
            logger.warning('Native referral action unavailable: %s', type(exc).__name__)
            raise HTTPException(503, detail={'code': 'temporarily_unavailable',
                'message': 'Не удалось проверить запрос. Попробуйте ещё раз: сохранённые заявки не потеряются.'}) from exc

    @router.post('/purchases')
    async def purchase(request: Request):
        user = await auth_user(request)
        payload = await body(request)
        return await run(actions.create_purchase(int(user['id']), payload, bot_provider()))

    @router.get('/purchases/{request_key}')
    async def purchase_status(request_key: str, request: Request):
        user = await auth_user(request)
        return await run(actions.get_purchase(int(user['id']), request_key, bot_provider()))

    @router.post('/withdrawals')
    async def withdrawal(request: Request):
        user = await auth_user(request)
        payload = await body(request)
        return await run(actions.create_withdrawal(int(user['id']), payload))

    @router.get('/withdrawals/{request_key}')
    async def withdrawal_status(request_key: str, request: Request):
        user = await auth_user(request)
        return await run(actions.get_withdrawal(int(user['id']), request_key))

    return router
