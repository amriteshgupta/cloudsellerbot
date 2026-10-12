import asyncio
import contextlib
import fcntl
import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path

from aiogram import BaseMiddleware, Bot, Dispatcher, Router
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage, SimpleEventIsolation
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from .config import Config
from .db import Store
from .policy import money, price_cents, validate_text

log = logging.getLogger(__name__)


def keyboard(buttons):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=label, callback_data=data)] for label, data in buttons]) if buttons else None


def utc(ts):
    return datetime.fromtimestamp(ts, timezone.utc).strftime('%Y-%m-%d %H:%M UTC')


class Submission(StatesGroup):
    entry = State()


class Buyer(StatesGroup):
    selection = State()


FIELDS = [
    ('name', 'Product name (3–100 characters):', 100),
    ('total', 'Initial quantity (1–10000):', 10),
    ('seller_price', 'Asking price per unit (e.g. 400.00):', 30),
    ('negotiable', 'Is the price negotiable? Reply YES or NO:', 3),
    ('delivery', 'Delivery time in hours (for example, 24):', 6),
]


class Access(BaseMiddleware):
    def __init__(self, store):
        self.store = store

    async def __call__(self, handler, event, data):
        message = event.message if isinstance(event, CallbackQuery) else event
        if not isinstance(message, Message) or message.chat.type != 'private':
            if isinstance(event, CallbackQuery):
                await event.answer('Use the bot in a private chat.', show_alert=True)
            return
        user = event.from_user
        if not user:
            return
        self.store.register(user.id, user.username)
        u = self.store.one('SELECT banned FROM users WHERE id=?', (user.id,))
        if u['banned'] and user.id not in self.store.admins:
            if isinstance(event, CallbackQuery):
                await event.answer('Trading access is disabled.', show_alert=True)
            else:
                await message.answer('Trading access is disabled. Contact the admin.')
            return
        try:
            return await handler(event, data)
        except (ValueError, OverflowError) as exc:
            if isinstance(event, CallbackQuery):
                await event.answer(str(exc)[:180], show_alert=True)
            else:
                await message.answer(str(exc)[:500])
        except Exception:
            # No event payloads or credentials in logs.
            log.error('Handler failed', exc_info=False)
            if isinstance(event, CallbackQuery):
                with contextlib.suppress(Exception):
                    await event.answer('Could not finish. Refresh your dashboard before retrying.', show_alert=True)
            else:
                await message.answer('Could not finish. Check /orders or /myproducts before retrying.')


class SendLimiter:
    """Throttle every sendMessage, including replies, against one global/per-chat clock."""
    def __init__(self, rate):
        self.lock = asyncio.Lock()
        self.interval = 1 / rate
        self.next_global = 0.0
        self.chats = {}

    async def __call__(self, make_request, bot, method):
        if method.__api_method__ != 'sendMessage':
            return await make_request(bot, method)
        async with self.lock:
            now = time.monotonic()
            delay = max(self.next_global, self.chats.get(method.chat_id, 0)) - now
            if delay > 0:
                await asyncio.sleep(delay)
            now = time.monotonic()
            self.next_global = now + self.interval
            self.chats[method.chat_id] = now + 1.05
            if len(self.chats) > 10000:
                self.chats = {k:v for k,v in self.chats.items() if v > now}
            try:
                return await make_request(bot, method)
            except TelegramRetryAfter as exc:
                self.next_global = max(self.next_global, time.monotonic() + exc.retry_after + 1)
                raise


def build_router(store, cfg):
    router = Router()
    access = Access(store)
    router.message.outer_middleware(access)
    router.callback_query.outer_middleware(access)

    def admin(uid):
        store.admin(uid)

    @router.message(Command('start','help'))
    async def start(m: Message, state: FSMContext):
        await state.clear()
        await m.answer('Marketplace help\n'
                       '/sell — list a product for admin approval\n'
                       '/buy — view available products and contact the admin\n'
                       '/edit — update one of your products\n'
                       '/cancel — stop the current form\n'
                       'Product listings are public only after admin approval.')

    @router.message(Command('cancel'))
    async def cancel_form(m: Message,state: FSMContext):
        await state.clear()
        await m.answer('Form discarded. Existing submitted listings and orders are unchanged.')

    async def begin_form(m, state, edit_id=None):
        u=store.one('SELECT * FROM users WHERE id=?',(m.from_user.id,))
        if not u['seller']:
            store.set_user(m.from_user.id,'seller',True)
        if edit_id:
            p=store.product(edit_id)
            if not p or p['seller_id']!=m.from_user.id or p['status'] not in ('pending','changes','rejected','approved','paused'):
                raise ValueError('Only your submitted listings can be edited.')
        await state.clear()
        await state.set_state(Submission.entry)
        await state.update_data(index=0, product={}, edit_id=edit_id)
        await m.answer('Enter each field in a separate message. /cancel discards this form.\n' + FIELDS[0][1])

    @router.message(Command('sell'))
    async def sell(m: Message,state: FSMContext):
        await begin_form(m,state)

    @router.message(Command('edit'))
    async def edit(m: Message,state: FSMContext):
        parts=m.text.split()
        if len(parts)==1:
            rows=store.rows('SELECT id,name,status FROM products WHERE seller_id=? ORDER BY id DESC',(m.from_user.id,))
            if not rows:
                await m.answer('You have no products. Use /sell to create one.')
                return
            await m.answer('Choose a product to edit:', reply_markup=keyboard([
                (f'Edit #{p["id"]} {p["name"]}', f'edit:{p["id"]}') for p in rows]))
            return
        if len(parts)!=2:
            raise ValueError('Usage: /edit PRODUCT_ID')
        await begin_form(m,state,int(parts[1]))

    @router.message(Command('buy'))
    async def buy(m: Message,state: FSMContext):
        if len(m.text.split())!=1:
            raise ValueError('Use /buy to see the available product list.')
        rows=store.rows('''SELECT p.* FROM products p JOIN users u ON u.id=p.seller_id
              WHERE p.status='approved' AND u.banned=0 AND p.total-p.held-p.sold>0 ORDER BY p.id DESC''')
        if not rows:
            await m.answer('There are no approved products currently available.')
            return
        await state.set_state(Buyer.selection)
        await state.update_data(buy_products=[p['id'] for p in rows])
        lines=['Available products:']
        for number,p in enumerate(rows,1):
            available=p['total']-p['held']-p['sold']
            lines.append(f'{number}. {p["name"]} | stock: {available} | '
                         f'{money(p["buyer_price"],cfg.currency)} each | '
                         f'negotiable: {"yes" if p["negotiable"] else "no"} | delivery: {p["delivery"]} hours')
        lines.append('Reply with the product number to contact the admin.')
        await m.answer('\n'.join(lines))

    @router.message(Command('admin'))
    async def panel(m: Message):
        admin(m.from_user.id)
        await m.answer('Admin controls\n/pending [PAGE] — product review\n/review PRODUCT_ID — full private listing\n'
                       '/reject ID reason\n/changes ID reason\n/pause ID reason\n/resume ID\n'
                       '/ban USER_ID\n/unban USER_ID\n/history PRODUCT_ID\n/outbox — failed notifications\n'
                       'Review products before approval. Buyer deals and delivery are arranged manually.')

    async def review(m,pid):
        p=store.product(pid)
        if not p:
            raise ValueError('Listing missing.')
        buttons=[]
        if p['status']=='pending':
            buttons=[('Approve: rights verified',f'approve:{pid}-{p["revision"]}')]
        await m.answer(store.review_text(p)+f'\n/reject {pid} reason\n/changes {pid} reason',reply_markup=keyboard(buttons))

    @router.message(Command('review'))
    async def review_cmd(m: Message):
        admin(m.from_user.id)
        parts=m.text.split()
        if len(parts)!=2:
            raise ValueError('Usage: /review PRODUCT_ID')
        await review(m,int(parts[1]))

    @router.message(Command('pending'))
    async def admin_list(m: Message):
        admin(m.from_user.id)
        parts=m.text.split()
        command=parts[0].split('@')[0]
        page=int(parts[1]) if len(parts)==2 else 0
        if page<0:
            raise ValueError('Page must be >= 0.')
        rows=store.rows('SELECT id,name FROM products WHERE status="pending" ORDER BY id LIMIT 10 OFFSET ?',(page*10,))
        for p in rows:
            await m.answer(f"Listing #{p['id']} {p['name']}",reply_markup=keyboard([('Review',f'review:{p["id"]}')]))
        await m.answer(f'Page {page}. {command} {page+1} for more.' if rows else 'Nothing on this page.')

    @router.message(Command('reject','changes','pause','resume'))
    async def moderation(m: Message):
        admin(m.from_user.id)
        parts=m.text.split(maxsplit=2)
        command=parts[0][1:].split('@')[0]
        if len(parts)<2 or command in ('reject','changes','pause') and len(parts)!=3:
            raise ValueError(f'Usage: /{command} PRODUCT_ID'+(' reason' if command!='resume' else ''))
        note=parts[2] if len(parts)==3 else ''
        if len(note)>800:
            raise ValueError('Reason must be <=800 characters.')
        store.moderate(m.from_user.id,int(parts[1]),command,note)
        await m.answer('Listing updated.')

    @router.message(Command('ban','unban'))
    async def ban(m: Message):
        admin(m.from_user.id)
        parts=m.text.split()
        if len(parts)!=2:
            raise ValueError('Usage: /ban USER_ID or /unban USER_ID')
        store.ban(m.from_user.id,int(parts[1]),parts[0].split('@')[0]=='/ban')
        await m.answer('User permissions updated.')

    @router.message(Command('history'))
    async def history(m: Message):
        admin(m.from_user.id)
        parts=m.text.split()
        if len(parts)!=2:
            raise ValueError('Usage: /history PRODUCT_ID')
        rows=store.rows('SELECT * FROM inventory_events WHERE product_id=? ORDER BY id DESC LIMIT 20',(int(parts[1]),))
        await m.answer('\n'.join(f"{utc(r['created'])}: {r['kind']} {r['quantity']}, order {r['order_id'] or '-'}, actor {r['actor_id']}" for r in rows) or 'No history.')

    @router.message(Command('outbox'))
    async def outbox(m: Message):
        admin(m.from_user.id)
        rows=store.rows('SELECT id,chat_id,state,attempts,last_error FROM outbox WHERE state="failed" ORDER BY id DESC LIMIT 20')
        await m.answer('\n'.join(f"Notification #{r['id']} to {r['chat_id']}: {r['state']}, attempts {r['attempts']} ({r['last_error']})" for r in rows) or 'No failed notifications.')

    @router.callback_query()
    async def callbacks(q: CallbackQuery,state: FSMContext):
        uid=q.from_user.id
        parts=(q.data or '').split(':')
        if len(parts)!=2:
            raise ValueError('Invalid action.')
        action,value=parts
        if action=='edit':
            await q.answer()
            message=q.message.model_copy(update={'from_user':q.from_user})
            await begin_form(message,state,int(value))
            return
        if action=='approve':
            pid, revision = value.split('-')
            number, revision = int(pid), int(revision)
        else:
            number=int(value)
        if action in ('review','approve'):
            admin(uid)
        if action=='approve':
            store.moderate(uid,number,'approve',expected_revision=revision)
            await q.answer('Approved.')
        elif action=='review':
            await q.answer()
            await review(q.message,number)
        else:
            raise ValueError('Unknown action.')

    @router.message(Buyer.selection)
    async def buyer_selection(m: Message,state: FSMContext):
        data=await state.get_data()
        try:
            number=int((m.text or '').strip())
            pid=data['buy_products'][number-1]
        except (ValueError, IndexError, KeyError, TypeError):
            raise ValueError('Reply with one of the product numbers shown by /buy, or use /cancel.')
        product=store.contact_admin(m.from_user.id,pid)
        await state.clear()
        await m.answer(f'Your interest in "{product["name"]}" was sent to the admin. '
                       'The admin will contact you to arrange quantity, payment and delivery.')

    @router.message(Submission.entry)
    async def submission(m: Message,state: FSMContext):
        data=await state.get_data()
        i=data['index']
        field,prompt,max_length=FIELDS[i]
        text=(m.text or '').strip()
        if not text or text.startswith('/'):
            raise ValueError('Send text for the current field, or /cancel.')
        if len(text)>max_length:
            raise ValueError(f'Maximum {max_length} characters for this field.')
        if field=='seller_price':
            value=price_cents(text)
        elif field=='total':
            value=int(text)
            if not 1<=value<=10000:
                raise ValueError('Quantity must be 1–10000.')
        elif field=='negotiable':
            if text.upper() not in ('YES','NO'):
                raise ValueError('Reply YES or NO.')
            value=text.upper()=='YES'
        elif field=='delivery':
            value=int(text)
            if not 1<=value<=8760:
                raise ValueError('Delivery time must be between 1 and 8760 hours.')
        else:
            validate_text(text)
            value=text
        product=data['product']
        product[field]=value
        if i==len(FIELDS)-1:
            product.update({
                'category':'General',
                'description':f'Product listing: {product["name"]}',
                'authorization':'Seller confirmed the product details for admin review.',
                'validity':'Not specified',
                'delivery':str(product['delivery']),
            })
            pid=store.submit(m.from_user.id,product,data.get('edit_id'))
            await state.clear()
            await m.answer(f'Listing #{pid} submitted for admin approval. It will appear in /buy after approval.')
            return
        await state.update_data(product=product,index=i+1)
        await m.answer(FIELDS[i+1][1])

    @router.message()
    async def fallback(m: Message):
        await m.answer('Use /help to see commands. Product submissions accept text only; never upload secrets.')

    return router


async def background(bot,store):
    """Process the durable notification outbox with bounded retries."""
    while True:
        try:
            job=store.one('SELECT * FROM outbox WHERE state="pending" AND next_attempt<=? ORDER BY id LIMIT 1',(time.time(),))
            if not job:
                await asyncio.sleep(0.5)
                continue
            body=job['body']
            try:
                await bot.send_message(job['chat_id'],body,reply_markup=keyboard(json.loads(job['buttons'])))
            except TelegramRetryAfter as exc:
                with store.tx() as c:
                    c.execute('UPDATE outbox SET next_attempt=?,last_error="rate limited" WHERE id=?',(time.time()+exc.retry_after+1,job['id']))
                await asyncio.sleep(min(exc.retry_after+1,5))
            except TelegramForbiddenError:
                with store.tx() as c:
                    c.execute('UPDATE outbox SET state="failed",last_error="blocked or forbidden" WHERE id=?',(job['id'],))
            except TelegramBadRequest:
                with store.tx() as c:
                    c.execute('UPDATE outbox SET state="failed",last_error="invalid Telegram request" WHERE id=?',(job['id'],))
            except Exception as exc:
                attempts=job['attempts']+1
                with store.tx() as c:
                    c.execute('UPDATE outbox SET attempts=?,next_attempt=?,state=?,last_error=? WHERE id=?',
                              (attempts,time.time()+min(300,2**attempts),'failed' if attempts>=10 else 'pending',type(exc).__name__,job['id']))
            else:
                with store.tx() as c:
                    c.execute('UPDATE outbox SET state="sent" WHERE id=?',(job['id'],))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.error('Background task error: %s',type(exc).__name__)
            await asyncio.sleep(5)


async def run():
    cfg=Config.load()
    store=Store(cfg.database,cfg.admins,cfg.currency)
    # Linux/macOS lock: only one poller/outbox worker per database. Released on crash.
    lock=Path(cfg.database+'.lock').open('a')
    try:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    except BlockingIOError:
        lock.close()
        raise RuntimeError('Another instance is already using this database.') from None
    try:
        async with Bot(cfg.token) as bot:
            bot.session.middleware(SendLimiter(cfg.broadcast_rate))
            dp=Dispatcher(storage=MemoryStorage(),events_isolation=SimpleEventIsolation())
            dp.include_router(build_router(store,cfg))
            worker=asyncio.create_task(background(bot,store))
            try:
                await dp.start_polling(bot,allowed_updates=['message','callback_query'],close_bot_session=False)
            finally:
                worker.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await worker
                await dp.storage.close()
    finally:
        lock.close()


def main():
    logging.basicConfig(level=logging.INFO,format='%(asctime)s %(levelname)s %(name)s: %(message)s')
    # Telegram client logs can contain private user data; no HTTP request debug logging.
    asyncio.run(run())


if __name__=='__main__':
    main()
