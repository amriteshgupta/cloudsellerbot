import asyncio
import tempfile
import time
import unittest
from pathlib import Path
from aiogram import Bot, Dispatcher
from aiogram.client.session.base import BaseSession
from aiogram.exceptions import TelegramRetryAfter
from aiogram.fsm.storage.memory import MemoryStorage, SimpleEventIsolation
from aiogram.methods import AnswerCallbackQuery, GetMe, SendMessage
from aiogram.types import Message, Update, User
from marketplace.bot import SendLimiter, build_router
from marketplace.config import Config
from marketplace.db import Store
from test_store import product


class FakeSession(BaseSession):
    def __init__(self):
        super().__init__()
        self.sent=[]
        self.answers=[]
        self.raise_retry=False

    async def close(self):
        pass

    async def make_request(self,bot,method,timeout=None):
        if isinstance(method,GetMe):
            return User(id=987654,is_bot=True,first_name='Marketplace',username='market_test_bot')
        if isinstance(method,SendMessage):
            if self.raise_retry:
                self.raise_retry=False
                raise TelegramRetryAfter(method=method,message='Too many requests',retry_after=1)
            if len(method.text)>4096:
                raise AssertionError('Message too long')
            self.sent.append(method)
            return Message(message_id=len(self.sent),date=int(time.time()),chat={'id':method.chat_id,'type':'private'},text=method.text)
        if isinstance(method,AnswerCallbackQuery):
            self.answers.append(method)
            return True
        raise AssertionError(f'Unexpected method: {type(method).__name__}')

    async def stream_content(self,url,**kwargs):
        if False:
            yield b''


class BotTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.db=Store(Path(self.tmp.name)/'bot.sqlite3',{1})
        self.cfg=Config('987654:'+'x'*35,frozenset({1}),self.db.path,'USD',10)
        self.session=FakeSession()
        self.bot=Bot(self.cfg.token,session=self.session)
        self.dp=Dispatcher(storage=MemoryStorage(),events_isolation=SimpleEventIsolation())
        self.dp.include_router(build_router(self.db,self.cfg))
        self.update_id=0

    async def asyncTearDown(self):
        await self.dp.storage.close()
        await self.bot.session.close()
        self.tmp.cleanup()

    async def message(self,uid,text,chat_type='private'):
        self.update_id+=1
        payload={'update_id':self.update_id,'message':{'message_id':self.update_id,'date':int(time.time()),
            'chat':{'id':uid,'type':chat_type},'from':{'id':uid,'is_bot':False,'first_name':f'User{uid}'},'text':text}}
        if text.startswith('/'):
            payload['message']['entities']=[{'type':'bot_command','offset':0,'length':len(text.split()[0])}]
        await self.dp.feed_update(self.bot,Update.model_validate(payload))

    async def callback(self,uid,data):
        self.update_id+=1
        await self.dp.feed_update(self.bot,Update.model_validate({'update_id':self.update_id,'callback_query':{
            'id':str(self.update_id),'chat_instance':'test','data':data,
            'from':{'id':uid,'is_bot':False,'first_name':f'User{uid}'},
            'message':{'message_id':500,'date':int(time.time()),'chat':{'id':uid,'type':'private'},
                       'from':{'id':987654,'is_bot':True,'first_name':'Marketplace'},'text':'Buttons'}}}))

    async def setup_listing(self):
        self.db.register(2,'seller')
        self.db.set_user(2,'seller',True)
        return self.db.submit(2,product())

    async def test_seller_form_admin_approval_buy_completion(self):
        await self.message(2,'/sell')
        self.assertIn('Product name',self.session.sent[-1].text)
        for value in ('Design service','5','400.00','YES','24'):
            await self.message(2,value)
        p=self.db.one('SELECT * FROM products')
        self.assertIsNotNone(p)
        self.assertEqual(p['seller_id'],2)
        self.assertEqual(p['negotiable'],1)
        self.assertEqual(p['delivery'],'24')
        self.assertEqual(p['status'],'pending')
        self.db.register(2,'monsto','Monsto')
        await self.message(1,'/review 1')
        self.assertIn('Seller username: @monsto',self.session.sent[-1].text)
        button=self.session.sent[-1].reply_markup.inline_keyboard[0][0]
        self.assertEqual(button.callback_data,'approve:1-1')
        await self.callback(1,button.callback_data)
        self.assertEqual(self.db.product(1)['status'],'approved')
        await self.message(3,'/buy')
        self.assertIn('$440.00',self.session.sent[-1].text)
        self.assertIn('stock: 5',self.session.sent[-1].text)
        await self.message(3,'1')
        self.assertIn('interest in "Design service"',self.session.sent[-1].text)
        notice=self.db.one('SELECT body FROM outbox WHERE chat_id=1 ORDER BY id DESC')
        self.assertIn('Buyer 3 is interested',notice['body'])

    async def test_edit_approved_listing_requires_fresh_approval(self):
        pid=await self.setup_listing()
        self.db.moderate(1,pid,'approve')
        await self.message(2,f'/edit {pid}')
        for value in ('Updated product','8','500.00','NO','48'):
            await self.message(2,value)
        self.assertEqual(self.db.product(pid)['status'],'pending')
        self.assertEqual(self.db.product(pid)['total'],8)
        self.assertEqual(self.db.product(pid)['revision'],2)

    async def test_forged_callbacks_and_order_privacy(self):
        pid=await self.setup_listing()
        await self.callback(3,f'approve:{pid}-1')
        self.assertEqual(self.db.product(pid)['status'],'pending')
        self.assertIn('Admin permission',self.session.answers[-1].text)
        await self.callback(3,f'review:{pid}')
        self.assertIn('Admin permission',self.session.answers[-1].text)
        self.db.moderate(1,pid,'approve')
        await self.message(3,'/buy')
        await self.message(3,'1')
        await self.message(4,'/order 1')
        self.assertIn('/help',self.session.sent[-1].text)
        self.assertIsNone(self.db.one('SELECT * FROM orders'))

    async def test_stale_approval_and_group_rejection(self):
        pid=await self.setup_listing()
        self.db.submit(2,product(8),pid)
        await self.callback(1,f'approve:{pid}-1')
        self.assertEqual(self.db.product(pid)['status'],'pending')
        await self.callback(1,f'approve:{pid}-2')
        self.assertEqual(self.db.product(pid)['status'],'approved')
        count=len(self.session.sent)
        await self.message(7,'/sell',chat_type='group')
        self.assertEqual(count,len(self.session.sent))
        self.assertIsNone(self.db.one('SELECT * FROM users WHERE id=7'))

    async def test_rate_limit_cooldown_applies_across_chats(self):
        self.bot.session.middleware(SendLimiter(20))
        self.session.raise_retry=True
        begin=time.monotonic()
        with self.assertRaises(TelegramRetryAfter):
            await self.bot.send_message(10,'Limited')
        await self.bot.send_message(11,'Different chat waits too')
        self.assertGreaterEqual(time.monotonic()-begin,1.9)

    async def test_limiter_applies_to_same_chat(self):
        self.bot.session.middleware(SendLimiter(20))
        begin=time.monotonic()
        await self.bot.send_message(10,'First')
        await self.bot.send_message(10,'Second')
        self.assertGreaterEqual(time.monotonic()-begin,1.0)


if __name__=='__main__':
    unittest.main()
