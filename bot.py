import asyncio, json, os
from pathlib import Path
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

ROOT=Path(__file__).parent; load_dotenv(ROOT/'.env')
TOKEN=os.environ['TELEGRAM_BOT_TOKEN']; AGY=os.getenv('AGY_PATH','agy')
WORK=Path(os.getenv('AGY_WORKSPACE','/srv/agy-workspace')).resolve()
TIMEOUT=int(os.getenv('AGY_TIMEOUT_SECONDS','900'))
ALLOWED={int(x) for x in os.getenv('ALLOWED_USER_IDS','').split(',') if x.strip()}
RUNNING={}
def ok(u): return bool(u.effective_user and u.effective_user.id in ALLOWED)
async def show_id(u,c):
    if u.effective_chat.type=='private': await u.message.reply_text(f'你的 Telegram 数字 ID：{u.effective_user.id}')
async def help_(u,c):
    if ok(u): await u.message.reply_text('直接发送任务给 agy。\n/status 状态  /cancel 停止')
async def status(u,c):
    if ok(u): await u.message.reply_text('任务运行中。' if u.effective_user.id in RUNNING else '当前没有任务。')
async def cancel(u,c):
    p=RUNNING.get(u.effective_user.id) if ok(u) else None
    if p: p.terminate(); await u.message.reply_text('已请求停止任务。')
async def task(u,c):
    if not ok(u) or u.effective_chat.type!='private': return
    text=(u.message.text or '').strip()
    if not text: return
    note=await u.message.reply_text('任务已接收，正在调用 agy…')
    try:
        cmd=[AGY,'--print-timeout',f'{TIMEOUT}s','--output-format','stream-json','--add-dir',str(WORK),f'--print={text}']
        p=await asyncio.create_subprocess_exec(*cmd,cwd=WORK,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE); RUNNING[u.effective_user.id]=p
        out,err=await asyncio.wait_for(p.communicate(),timeout=TIMEOUT+30); answer=''
        for line in out.decode(errors='replace').splitlines():
            try: answer=json.loads(line).get('result',{}).get('response',answer)
            except json.JSONDecodeError: pass
        await note.edit_text('任务完成：')
        for i in range(0,len(answer) or 1,3900): await u.message.reply_text(answer[i:i+3900] or 'agy 没有返回内容。')
    except Exception as e: await note.edit_text(f'执行失败：{e}')
    finally: RUNNING.pop(u.effective_user.id,None)
def main():
    app=Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler('id',show_id)); app.add_handler(CommandHandler(['start','help'],help_))
    app.add_handler(CommandHandler('status',status)); app.add_handler(CommandHandler('cancel',cancel))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE,task)); app.run_polling()
if __name__=='__main__': main()
