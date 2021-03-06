from telegram.ext.dispatcher import run_async
from telegram.ext import Updater, MessageHandler, Filters, CommandHandler, CallbackContext
from telegram import ChatAction, Update
import logging
import re
import sys
from time import sleep
from random import uniform
from queue import Queue
import os.path
import subprocess
from cachetools import TTLCache, cached

from configuration import Config
from database import dbcur_queryone, with_cursor, cache_on_commit
import threads
from httpnn import HTTPNN
import asyncio
from concurrent.futures import ThreadPoolExecutor
from util import retry, inqueue

if len(sys.argv) != 2:
  raise Exception("Wrong number of arguments")
Config.read(sys.argv[1])

known_stickers = set()
downloaded_files = set()
downloadqueue = Queue(maxsize=1024)
logqueue = Queue()
cmdqueue = Queue()
options = {}
sticker_emojis = None
pqed_messages = set()
command_replies = set()
last_msg_id = {}
logger = logging.getLogger(__file__)


def put(convid, text):
  nn.run_from_thread(nn.put, str(convid), text)

async def get_cb_as(callback, convid, bad_words):
  text = await nn.get(str(convid), bad_words)
  await asyncio.get_event_loop().run_in_executor(None, callback, text)

def get_cb(callback, convid, bad_words):
  nn.run_from_thread(get_cb_as, callback, convid, bad_words)

def user_name(user):
  if user.username:
    return user.username
  return '(' + user.first_name + ')'

def chatname(chat):
  try:
    if chat.title:
      return chat.title
    else:
      n = chat.first_name
      if chat.last_name:
        n = n + ' ' + chat.last_name
      return n
  except:
    logger.exception("can't get name:")
    return '<err>'

def lookup_sticker_emoji(emoji):
  if emoji in sticker_emojis:
    return emoji
  emoji = emoji.strip(u'\ufe00\ufe01\ufe02\ufe03\ufe04\ufe05\ufe06\ufe07\ufe09\ufe0a\ufe0b\ufe0c\ufe0d\ufe0e\ufe0f')
  if emoji in sticker_emojis:
    return emoji
  return None

chatinfo_last = {}
def update_chatinfo_current(cur, convid, chatinfo_id):
  if (convid not in chatinfo_last) or (chatinfo_last[convid] != chatinfo_id):
    logger.info("Updating chatinfo_current: %d -> %d" % (convid, chatinfo_id))
    cur.execute("REPLACE INTO chatinfo_current (convid, chatinfo_id) VALUES (%s, %s)", (convid, chatinfo_id))
    cache_on_commit(cur, chatinfo_last, convid, chatinfo_id)

chatinfo_cache = {}
def get_chatinfo_id(cur, chat):
  if chat is None:
    return None
  metadata = (chat.id, chat.username, chat.first_name, chat.last_name, getattr(chat, 'title', None))
  if metadata in chatinfo_cache:
    update_chatinfo_current(cur, chat.id, chatinfo_cache[metadata])
    return chatinfo_cache[metadata]
  cur.execute("SELECT chatinfo_id FROM chatinfo WHERE chat_id <=> %s AND username <=> %s AND first_name <=> %s AND last_name <=> %s AND title <=> %s FOR UPDATE", metadata)
  res = cur.fetchall()
  if res:
    cache_on_commit(cur, chatinfo_cache, metadata, res[0][0])
    update_chatinfo_current(cur, chat.id, res[0][0])
    return res[0][0]
  cur.execute("INSERT INTO chatinfo (chat_id, username, first_name, last_name, title) VALUES (%s, %s, %s, %s, %s)", metadata)
  rid = cur.lastrowid
  cache_on_commit(cur, chatinfo_cache, metadata, rid)
  update_chatinfo_current(cur, chat.id, rid)
  return rid

@inqueue(logqueue)
@retry(10)
@with_cursor
def log(cur, sent, text, original_message = None, msg_id = None, reply_to_id = None, conversation=None, user=None, rowid_out = None, fwduser = None, fwdchat = None):
  chatinfo_id = get_chatinfo_id(cur, conversation)
  userinfo_id = get_chatinfo_id(cur, user)
  fwduser_id = get_chatinfo_id(cur, fwduser)
  fwdchat_id = get_chatinfo_id(cur, fwdchat)
  conv = conversation.id
  fromid = user.id
  cur.execute("INSERT INTO `chat` (`convid`, `fromid`, `sent`, `text`, `msg_id`, chatinfo_id, userinfo_id) VALUES (%s, %s, %s, %s, %s, %s, %s)", (conv, fromid, sent, text, msg_id, chatinfo_id, userinfo_id))
  rowid = cur.lastrowid
  if original_message:
    cur.execute("INSERT INTO `chat_original` (`id`, `original_text`) VALUES (LAST_INSERT_ID(), %s)", (original_message,))
  if reply_to_id:
    cur.execute("INSERT INTO `replies` (`id`, `reply_to`) VALUES (LAST_INSERT_ID(), %s)", (reply_to_id,))
  if fwduser or fwdchat:
    cur.execute("INSERT INTO `forwarded_from` (`id`, `fwd_userinfo_id`, `fwd_chatinfo_id`) VALUES (LAST_INSERT_ID(), %s, %s)", (fwduser_id, fwdchat_id))
  if rowid_out is not None:
    rowid_out.append(rowid)
  return rowid

@inqueue(logqueue)
@retry(10)
@with_cursor
def log_cmd(cur, cmd, conversation = None, user = None):
  chatinfo_id = get_chatinfo_id(cur, conversation)
  userinfo_id = get_chatinfo_id(cur, user)
  conv = conversation.id
  cur.execute("INSERT INTO `commands` (`convid`, `command`, chatinfo_id, userinfo_id) VALUES (%s, %s, %s, %s)", (conv, cmd, chatinfo_id, userinfo_id))

@inqueue(logqueue)
@retry(10)
@with_cursor
def log_sticker(cur, sent, text, file_id, set_name, msg_id = None, reply_to_id = None, conversation=None, user=None, rowid_out = None, fwduser = None, fwdchat = None):
  chatinfo_id = get_chatinfo_id(cur, conversation)
  userinfo_id = get_chatinfo_id(cur, user)
  fwduser_id = get_chatinfo_id(cur, fwduser)
  fwdchat_id = get_chatinfo_id(cur, fwdchat)
  conv = conversation.id
  fromid = user.id
  cur.execute("INSERT INTO `chat` (`convid`, `fromid`, `sent`, `text`, `msg_id`, chatinfo_id, userinfo_id) VALUES (%s, %s, %s, %s, %s, %s, %s)", (conv, fromid, sent, text, msg_id, chatinfo_id, userinfo_id))
  rowid = cur.lastrowid
  cur.execute("INSERT INTO `chat_sticker` (`id`, `file_id`, `set_name`) VALUES (LAST_INSERT_ID(), %s, %s)", (file_id, set_name))
  if reply_to_id:
    cur.execute("INSERT INTO `replies` (`id`, `reply_to`) VALUES (LAST_INSERT_ID(), %s)", (reply_to_id,))
  if fwduser or fwdchat:
    cur.execute("INSERT INTO `forwarded_from` (`id`, `fwd_userinfo_id`, `fwd_chatinfo_id`) VALUES (LAST_INSERT_ID(), %s, %s)", (fwduser_id, fwdchat_id))
  if file_id not in known_stickers:
    cur.execute("SELECT COUNT(*) FROM `stickers` WHERE `file_id` = %s", (file_id,))
    (exists,) = cur.fetchone()
    if exists == 0:
      logger.info("Adding sticker <%s> <%s> < %s >" % (file_id, set_name, text))
      cur.execute("REPLACE INTO `stickers` (`file_id`, `emoji`, `set_name`) VALUES (%s, %s, %s)", (file_id, text, set_name))
  if file_id not in known_stickers:
    known_stickers.add(file_id)
    sticker_emojis.add(text)
  if rowid_out is not None:
    rowid_out.append(rowid)
  return rowid

@inqueue(logqueue)
@retry(10)
@with_cursor
def log_add_msg_id(cur, db_id, msg_id):
  if isinstance(msg_id, list) and msg_id:
    msg_id = msg_id[0]
  cur.execute("UPDATE `chat` SET `msg_id`=%s WHERE `id`=%s AND msg_id IS NULL", (msg_id, db_id))

@inqueue(logqueue)
@retry(10)
@with_cursor
def log_file(cur, ftype, fsize, attr, file_id, conversation=None, user=None):
  chatinfo_id = get_chatinfo_id(cur, conversation)
  userinfo_id = get_chatinfo_id(cur, user)
  conv = conversation.id
  cur.execute("INSERT INTO `chat_files` (`convid`, `type`, `file_size`, `attr`, `file_id`, chatinfo_id, userinfo_id) VALUES (%s, %s, %s, %s, %s, %s, %s)", (conv, ftype, fsize, attr, file_id, chatinfo_id, userinfo_id))

@inqueue(logqueue)
@retry(10)
@with_cursor
def log_status(cur, updates, conversation=None, user=None):
  chatinfo_id = get_chatinfo_id(cur, conversation)
  userinfo_id = get_chatinfo_id(cur, user)
  conv = conversation.id
  if not updates:
    return
  for u in updates:
    member_id = get_chatinfo_id(cur, u[2])
    cur.execute("INSERT INTO `status_updates` (`convid`, `type`, `value`, chatinfo_id, userinfo_id, member_id) VALUES (%s, %s, %s, %s, %s, %s)", (conv, u[0], u[1], chatinfo_id, userinfo_id, member_id))

@inqueue(logqueue)
@retry(10)
@with_cursor
def log_migration(cur, newid, oldid):
  try:
    cur.execute("INSERT INTO `chat_migrations` (`newid`, `oldid`) VALUES (%s, %s)", (newid, oldid))
    cur.execute("UPDATE `badwords` SET `convid`=%s WHERE `convid`=%s", (newid, oldid))
    cur.execute("UPDATE `options` SET `convid`=%s WHERE `convid`=%s", (newid, oldid))
  except:
    logger.exception("Migration failed:")

@inqueue(logqueue)
@retry(10)
@with_cursor
def log_file_text(cur, fileid, texttype, filetext):
  cur.execute("REPLACE INTO `file_text` (`file_id`, `type`, `file_text`) VALUES (%s, %s, %s)", (fileid, texttype, filetext))

@retry(5)
@with_cursor
def rand_sticker(cur, emoji = None):
  if emoji:
    emoji = lookup_sticker_emoji(emoji)
    if not emoji:
      return None
    cur.execute("SELECT `file_id`, `emoji`, `set_name` FROM `stickers` WHERE `freqmod` > 0 AND `emoji` = %s ORDER BY -LOG(1.0 - RAND()) / `freqmod` LIMIT 1", (emoji,))
  else:
    cur.execute("SELECT `file_id`, `emoji`, `set_name` FROM `stickers` WHERE `freqmod` > 0 ORDER BY -LOG(1.0 - RAND()) / `freqmod` LIMIT 1")
  row = cur.fetchone()
  return row

@retry(5)
@with_cursor
def get_sticker_emojis(cur):
  cur.execute("SELECT DISTINCT `emoji` from `stickers` WHERE `freqmod` > 0")
  rows = cur.fetchall()
  return [x[0] for x in rows]

@retry(5)
@with_cursor
def already_pqd(cur, txt):
  cur.execute("SELECT COUNT(*) FROM `pq` WHERE `message` = %s", (txt,))
  (exists,) = cur.fetchone()
  if exists > 0:
    return True
  return False

@retry(5)
@with_cursor
def db_stats(cur, convid):
  recv = dbcur_queryone(cur, "SELECT message_count FROM `chat_counters` WHERE convid = %s AND sent=0", (convid,), 0)
  sent = dbcur_queryone(cur, "SELECT message_count FROM `chat_counters` WHERE convid = %s AND sent=1", (convid,), 0)
  firstdate = dbcur_queryone(cur, "SELECT MIN(`date`) FROM `chat` WHERE convid = %s", (convid,))
  rank = dbcur_queryone(cur, "SELECT COUNT(*)+1 FROM chat_counters WHERE SIGN(convid) = SIGN(%s) AND sent = 0 AND message_count > %s", (convid, recv))
  trecv = dbcur_queryone(cur, "SELECT value FROM `counters` WHERE name='count_recv'");
  tsent = dbcur_queryone(cur, "SELECT value FROM `counters` WHERE name='count_sent'");
  actusr = dbcur_queryone(cur, "SELECT COUNT(DISTINCT convid) FROM `chat_counters` WHERE convid > 0 AND last_date > DATE_SUB(NOW(), INTERVAL 48 HOUR)")
  actgrp = dbcur_queryone(cur, "SELECT COUNT(DISTINCT convid) FROM `chat_counters` WHERE convid < 0 AND last_date > DATE_SUB(NOW(), INTERVAL 48 HOUR)")
  quality = dbcur_queryone(cur, "SELECT uniqueness_rel FROM chat_uniqueness LEFT JOIN chat_uniqueness_rel USING (convid)  WHERE convid = %s AND last_count_valid >= 100", (convid,))
  return recv, sent, firstdate, rank, trecv, tsent, actusr, actgrp, quality

@inqueue(logqueue)
@retry(10)
@with_cursor
def log_pq(cur, convid, userid, txt):
  cur.execute("INSERT INTO `pq` (`convid`, `userid`, `message`) VALUES (%s, %s, %s)", (convid, userid, txt))

@retry(5)
@with_cursor
def pq_limit_check(cur, userid):
  cur.execute("SELECT COUNT(*) FROM pq WHERE userid=%s AND date > DATE_SUB(NOW(), INTERVAL 1 HOUR)", (userid,))
  res = cur.fetchone()[0]
  return res

@retry(5)
@with_cursor
def cmd_limit_check(cur, convid):
  cur.execute("SELECT COUNT(*) FROM commands WHERE convid=%s AND date > DATE_SUB(NOW(), INTERVAL 10 MINUTE) AND id > (SELECT MAX(id) - 1000 FROM commands)", (convid,))
  res = cur.fetchone()[0]
  return res

@retry(5)
@with_cursor
def option_set(cur, convid, option, value):
  cur.execute("REPLACE INTO `options` (`convid`, `option`, `value`) VALUES (%s,%s, %s)", (convid, option, str(value)))
  options[(convid, option)] = value

@retry(5)
@with_cursor
def option_get_raw(cur, convid, option):
  if (convid, option) in options:
    return options[(convid, option)]
  cur.execute("SELECT `value` FROM `options` WHERE `convid` = %s AND `option` = %s", (convid, option))
  row = cur.fetchone()
  if row != None:
    options[(convid, option)] = row[0]
    return row[0]
  else:
    return None

def option_get_float(convid, option, def_u, def_g):
  try:
    oraw = option_get_raw(convid, option)
    if oraw != None:
      return float(oraw)
  except:
    logger.exception("Error getting option %s for conv %d" % (option, convid))
  if convid > 0:
    return def_u
  else:
    return def_g

badword_cache = {}

@retry(5)
@with_cursor
def get_badwords(cur, convid):
  if convid in badword_cache:
    return badword_cache[convid]
  cur.execute("SELECT `badword` FROM `badwords` WHERE `convid` = %s", (convid,))
  r = [x[0] for x in cur]
  badword_cache[convid] = r
  return r

@retry(5)
@with_cursor
def add_badword(cur, convid, badword, by):
  cur.execute("INSERT INTO `badwords` (`convid`, `badword`, `addedby`) VALUES (%s, %s, %s)", (convid, badword, by))
  badword_cache[convid].append(badword)

@retry(5)
@with_cursor
def delete_badword(cur, convid, badword):
  cur.execute("DELETE FROM `badwords` WHERE `convid` = %s AND `badword` = %s", (convid, badword))
  badword_cache[convid].remove(badword)

@with_cursor
def db_get_photo(cur, fid):
  cur.execute("SELECT COUNT(*) FROM chat_files WHERE type = 'photo' AND file_id = %s", (fid,))
  return cur.fetchone()[0]

def setup_logging():
  verbose = Config.getboolean('Logging', 'VerboseStdout')
  console = logging.StreamHandler()
  console.setLevel(logging.INFO if verbose else logging.WARNING)
  logfile = logging.FileHandler(Config.get('Logging', 'Logfile'))
  logfile.setLevel(logging.INFO)
  logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO, handlers = [console, logfile])
setup_logging()

def ireplace(old, new, text):
  idx = 0
  while idx < len(text):
    index_l = text.lower().find(old.lower(), idx)
    if index_l == -1:
      return text
    text = text[:index_l] + new + text[index_l + len(old):]
    idx = index_l + len(new) 
  return text

def get_cache_key(bot, ci):
  return ci

@cached(TTLCache(1024, 60), key = get_cache_key)
def can_send_message(bot, ci):
  self_member = bot.get_chat_member(ci, bot.id)
  if self_member.status == 'restricted' and not self_member.can_send_messages:
    return False
  return True

@cached(TTLCache(1024, 600), key = get_cache_key)
def can_send_sticker(bot, ci):
  self_member = bot.get_chat_member(ci, bot.id)
  if self_member.status == 'restricted' and not self_member.can_send_other_messages:
    return False
  return True

@run_async
def send_typing_notification(bot, convid):
  try:
    bot.sendChatAction(chat_id=convid, action=ChatAction.TYPING)
  except Exception:
    logger.exception("Can't send typing action")

def sendreply(bot, ci, fro, froi, fron, replyto=None, replyto_cond=None, conversation = None, user=None):
  if asyncio.run_coroutine_threadsafe(nn.queued_for_key(str(ci)), nn.loop).result() > 16:
    logger.warning('Warning: reply queue full, dropping reply')
    return
  send_typing_notification(bot, ci)
  badwords = get_badwords(ci)
  badwords.sort(key=len, reverse=True)
  def rf(txt):
    omsg = msg = txt
    for bw in badwords:
      msg = ireplace(bw, '*' * len(bw), msg)
    logging.info(' => %s/%s/%d: %s' % (fron, fro, ci, msg))
    if omsg != msg:
      logging.info(' (original)=> %s' % (omsg,))
    sp = option_get_float(ci, 'sticker_prob', 0.9, 0)
    if (not replyto) and replyto_cond and (replyto_cond != last_msg_id[ci]):
      reply_to = replyto_cond
    else:
      reply_to = replyto
    last_msg_id[ci] = -1
    if uniform(0, 1) < sp and can_send_sticker(bot, ci):
      rs = rand_sticker(msg)
      if rs:
        logging.info('sending as sticker %s/%s' % (rs[2], rs[0]))
        dbid = []
        log_sticker(1, msg, rs[0], rs[2], reply_to_id = replyto_cond, conversation=conversation, user=user, rowid_out = dbid)
        try:
          m = bot.sendSticker(chat_id=ci, sticker=rs[0], reply_to_message_id = reply_to)
        except Exception:
          if reply_to:
            logger.exception("Can't send reply, trying without")
            m = bot.sendSticker(chat_id=ci, sticker=rs[0])
          else:
            raise
        log_add_msg_id(dbid, m.message_id)
        return
    dbid = []
    log(1, msg, original_message = omsg if omsg != msg else None, reply_to_id = replyto_cond, conversation=conversation, user=user, rowid_out = dbid)
    try:
      m = bot.sendMessage(chat_id=ci, text=msg, reply_to_message_id=reply_to)
    except Exception:
      if reply_to:
        logger.exception("Can't send reply, trying without")
        m = bot.sendMessage(chat_id=ci, text=msg)
      else:
        raise
    log_add_msg_id(dbid, m.message_id)
  get_cb(rf, ci, badwords)

def fix_name(value):
  value = re.sub('[/<>:"\\\\|?*]', '_', value)
  return value

def download_file(bot, ftype, fid, fname, on_finish=None):
  fname = fix_name(fname)

  def df():
    filename = ftype + '/' + fname
    if os.path.isfile(filename):
      logging.info('file ' + filename + ' already exists')
      if on_finish:
        on_finish(filename)
      return
    f = bot.getFile(file_id=fid)
    logging.info('downloading file ' + filename + ' from ' + f.file_path)
    f.download(custom_path=filename, timeout=120)
    if on_finish:
      on_finish(filename)
    sleep(3)
  if downloadqueue.full():
    logging.warning('Warning: download queue full')
    if not on_finish:
      return
  downloadqueue.put(df, True, 30)
  downloaded_files.add(fid)

def getmessage(bot, ci, fro, froi, fron, txt, msg_id, message):
  logging.info('%s/%s/%d: %s' % (fron, fro, ci, txt))
  put(ci, txt)

  reply_to_id = message.reply_to_message.message_id if message.reply_to_message else None
  conversation = message.chat
  user = message.from_user
  fwduser = message.forward_from
  fwdchat = message.forward_from_chat

  log(0, txt, msg_id=msg_id, reply_to_id=reply_to_id, conversation=conversation, user=user, fwduser=fwduser, fwdchat=fwdchat)

def cifrofron(update):
  ci = update.message.chat_id
  fro = user_name(update.message.from_user)
  fron = chatname(update.message.chat)
  froi = update.message.from_user.id
  return ci, fro, fron, froi

def should_reply(bot, msg, ci, txt = None):
  if msg and msg.reply_to_message and msg.reply_to_message.from_user.id == bot.id:
    return True and can_send_message(bot, ci)
  if not txt:
    txt = msg.text
  if txt and (Config.get('Chat', 'Keyword') in txt.lower()):
    return True and can_send_message(bot, ci)
  rp = option_get_float(ci, 'reply_prob', 1, 0.02)
  return (uniform(0, 1) < rp) and can_send_message(bot, ci)

def msg(update: Update, context: CallbackContext):
  if not update.message:
    return
  ci, fro, fron, froi = cifrofron(update)
  message = update.message
  txt = update.message.text
  last_msg_id[ci] = update.message.message_id
  getmessage(context.bot, ci, fro, froi, fron, txt, update.message.message_id, update.message)
  if should_reply(context.bot, update.message, ci):
    sendreply(context.bot, ci, fro, froi, fron, replyto_cond = update.message.message_id, conversation=update.message.chat, user = update.message.from_user)

def me(update: Update, context: CallbackContext):
  ci, fro, fron, froi = cifrofron(update)
  message = update.message
  txt = update.message.text
  last_msg_id[ci] = update.message.message_id
  getmessage(context.bot, ci, fro, froi, fron, txt, update.message.message_id, update.message)
  sendreply(context.bot, ci, fro, froi, fron, replyto_cond = update.message.message_id, conversation=update.message.chat, user = update.message.from_user)

def sticker(update: Update, context: CallbackContext):
  if not update.message:
    return
  ci, fro, fron, froi = cifrofron(update)
  message = update.message
  last_msg_id[ci] = update.message.message_id
  st = update.message.sticker
  set = '(unnamed)' if st.set_name is None else st.set_name
  emo = st.emoji or ''
  logging.info('%s/%s/%d: [sticker <%s> <%s> < %s >]' % (fron, fro, ci, st.file_id, set, emo))
  put(ci, emo)
  log_sticker(0, emo, st.file_id, set, msg_id = update.message.message_id, reply_to_id = update.message.reply_to_message.message_id if update.message.reply_to_message else None,
    fwduser = message.forward_from, fwdchat = message.forward_from_chat,
    conversation=update.message.chat, user=update.message.from_user)
  if should_reply(context.bot, update.message, ci):
    sendreply(context.bot, ci, fro, froi, fron, replyto_cond = update.message.message_id, conversation=update.message.chat, user = update.message.from_user)
  download_file(context.bot, 'stickers', st.file_id, st.file_id + ' ' + set + '.webp');

def video(update: Update, context: CallbackContext):
  if not update.message:
    return
  ci, fro, fron, froi = cifrofron(update)
  vid = update.message.video
  fid = vid.file_id
  attr = '%dx%d; length=%d; type=%s' % (vid.width, vid.height, vid.duration, vid.mime_type)
  size = vid.file_size
  logging.info('%s/%s: video, %d, %s, %s' % (fron, fro, size, fid, attr))
  if (Config.getboolean('Download', 'Video', fallback=True)):
    download_file(context.bot, 'video', fid, fid + '.mp4')
  log_file('video', size, attr, fid, conversation=update.message.chat, user=update.message.from_user)

def document(update: Update, context: CallbackContext):
  if not update.message:
    return
  ci, fro, fron, froi = cifrofron(update)
  doc = update.message.document
  fid = doc.file_id
  size = doc.file_size
  name = doc.file_name
  if not name:
    name = '_unnamed_.mp4'
  attr = 'type=%s; name=%s' % (doc.mime_type, name)
  logging.info('%s/%s: document, %d, %s, %s' % (fron, fro, size, fid, attr))
  if (Config.getboolean('Download', 'Document', fallback=True)):
    download_file(context.bot, 'document', fid, fid + ' ' + name)
  log_file('document', size, attr, fid, conversation=update.message.chat, user=update.message.from_user)

def audio(update: Update, context: CallbackContext):
  if not update.message:
    return
  ci, fro, fron, froi = cifrofron(update)
  aud = update.message.audio
  fid = aud.file_id
  size = aud.file_size
  ext = '.ogg'
  if aud.mime_type == 'audio/mp3':
    ext = '.mp3'
  attr = 'type=%s; duration=%d; performer=%s; title=%s' % (aud.mime_type, aud.duration, aud.performer, aud.title)
  logging.info('%s/%s: audio, %d, %s, %s' % (fron, fro, size, fid, attr))
  if (Config.getboolean('Download', 'Audio', fallback=True)):
    download_file(context.bot, 'audio', fid, '%s %s - %s%s' % (fid, aud.performer, aud.title, ext))
  log_file('audio', size, attr, fid, conversation=update.message.chat, user=update.message.from_user)

def photo(update: Update, context: CallbackContext):
  if not update.message:
    return
  ci, fro, fron, froi = cifrofron(update)
  message = update.message
  last_msg_id[ci] = update.message.message_id
  txt = update.message.caption
  photos = update.message.photo
  maxsize = 0
  pho = None
  for photo in photos:
    if photo.file_size > maxsize and photo.file_size < 20 * 1024 * 1024:
      maxsize = photo.file_size
      pho = photo
  fid = pho.file_id
  attr = 'dim=%dx%d' % (pho.width, pho.height)
  if txt:
    attr += '; caption=' + txt
    getmessage(context.bot, ci, fro, froi, fron, txt, update.message.message_id, update.message)
    if should_reply(context.bot, update.message, ci, txt):
      sendreply(context.bot, ci, fro, froi, fron, replyto = update.message.message_id, conversation=update.message.chat, user = update.message.from_user)
  logging.info('%s/%s: photo, %d, %s, %s' % (fron, fro, maxsize, fid, attr))
  def process_photo(f):
    logging.info('OCR running on %s' % f)
    ocrtext = subprocess.check_output(['tesseract', f, 'stdout']).decode('utf8', errors='ignore')
    ocrtext = re.sub('[\r\n]+', '\n',ocrtext).strip()
    logging.info('OCR: "%s"' % ocrtext)
    if ocrtext == "":
      return
    log_file_text(fid, 'ocr', ocrtext)
    def process_photo_reply(_context):
      put(ci, ocrtext)
      if (Config.get('Chat', 'Keyword') in ocrtext.lower()):
        logging.info('sending reply')
        sendreply(_context.bot, ci, fro, froi, fron, replyto=update.message.message_id, conversation=update.message.chat, user = update.message.from_user)
    updater.job_queue.run_once(process_photo_reply, 0)
  download_file(context.bot, 'photo', fid, fid + '.jpg', on_finish=process_photo)
  log_file('photo', maxsize, attr, fid, conversation=update.message.chat, user=update.message.from_user)

def cmd_download_photo(update: Update, context: CallbackContext):
  if not update.message:
    return
  fid = update.message.text.split(' ')[1]
  if db_get_photo(fid):
    download_file(context.bot, 'photo', fid, fid + '.jpg')
  else:
    logger.warning('Photo not in DB')

def voice(update: Update, context: CallbackContext):
  ci, fro, fron, froi = cifrofron(update)
  voi = update.message.voice
  fid = voi.file_id
  size = voi.file_size
  attr = 'type=%s; duration=%d' % (voi.mime_type, voi.duration)
  logging.info('%s/%s: voice, %d, %s, %s' % (fron, fro, size, fid, attr))
  download_file(context.bot, 'voice', fid, fid + '.opus')
  log_file('voice', size, attr, fid, conversation=update.message.chat, user=update.message.from_user)

def status(update: Update, context: CallbackContext):
  msg = update.message
  ci, fro, fron, froi = cifrofron(update)
  upd = []
  if msg.new_chat_members:
    for mmb in msg.new_chat_members:
      upd.append(('new_member', str(mmb.id) + ' ' + user_name(mmb), mmb))
  if msg.left_chat_member:
    mmb = msg.left_chat_member
    upd.append(('left_member', str(mmb.id) + ' ' + user_name(mmb), mmb))
  if msg.new_chat_title:
    upd.append(('new_title', msg.new_chat_title, None))
  if msg.group_chat_created:
    upd.append(('group_created', '', None))
  if msg.supergroup_chat_created:
    upd.append(('supergroup_created', '', None))
  if msg.migrate_from_chat_id:
    upd.append(('migrate_from_chat_id', str(msg.migrate_from_chat_id), None))
    log_migration(ci, msg.migrate_from_chat_id)
  for u in upd:
    logging.info('[UPDATE] %s / %s: %s  %s' % (fron, fro, u[0], u[1]))
  log_status(upd, conversation=update.message.chat, user=update.message.from_user)

def cmdreply(bot, ci, text):
  logging.info('=> %s' % text)
  msg = bot.sendMessage(chat_id=ci, text=text)
  command_replies.add(msg.message_id)

def givesticker(update: Update, context: CallbackContext):
  ci, fro, fron, froi = cifrofron(update)
  foremo = None
  cmd = update.message.text
  m = re.match('^/[^ ]+ (.+)', cmd)
  if m:
    foremo = m.group(1).strip()
  rs = rand_sticker(foremo)
  if not rs:
    cmdreply(context.bot, ci, '<no sticker for %s>\n%s' % (foremo, ''.join(list(sticker_emojis))))
  else:
    fid, emo, set = rs
    logging.info('%s/%s/%d: [giving random sticker: <%s> <%s>]' % (fron, fro, ci, fid, set))
    context.bot.sendSticker(chat_id=ci, sticker=fid)

def cmd_ratelimit(inf):
  def outf(update: Update, context: CallbackContext):
    if (cmd_limit_check(update.message.chat_id) > 100):
      logging.warning('rate limited!')
      return
    inf(update, context)
  return outf

@cmd_ratelimit
def start(update: Update, context: CallbackContext):
  ci, fro, fron, froi = cifrofron(update)
  logging.info('%s/%d /start' % (fro, ci))
  sendreply(context.bot, ci, fro, froi, fron, conversation=update.message.chat, user = update.message.from_user)

@cmd_ratelimit
def cmd_option_get(update: Update, context: CallbackContext):
  ci = update.message.chat_id
  txt = update.message.text.split()
  if (len(txt) != 2):
    cmdreply(context.bot, ci, '< invalid syntax >')
    return
  opt = txt[1]
  val = option_get_raw(ci, opt)
  if val == None:
    cmdreply(context.bot, ci, '<option %s not set>' % (opt,))
  else:
    cmdreply(context.bot, ci, '<option %s is set to %s>' % (opt, val))

def option_valid(o, v):
  if o == 'sticker_prob' or o == 'reply_prob' or o == 'admin_only':
    if re.match(r'^([0-9]+|[0-9]*\.[0-9]+)$', v):
      return True
    else:
      return False
  else:
    return False

def user_is_admin(bot, convid, userid):
  if convid > 0:
    return True
  member = bot.get_chat_member(convid, userid)
  if member.status == 'administrator' or member.status == 'creator':
    return True
  return False

def admin_check(bot, convid, userid):
  if option_get_float(convid, 'admin_only', 0, 0) == 0:
    return True
  return user_is_admin(bot, convid, userid)

@inqueue(cmdqueue)
@cmd_ratelimit
def cmd_option_set(update: Update, context: CallbackContext):
  ci = update.message.chat_id
  txt = update.message.text.split()
  if (len(txt) != 3):
    cmdreply(context.bot, ci, '< invalid syntax, use /option_set <option> <value> >')
    return
  if not admin_check(context.bot, ci, update.message.from_user.id):
     cmdreply(context.bot, ci, '< you are not allowed to use this command >')
     return
  opt = txt[1]
  val = txt[2]
  if option_valid(opt, val):
    option_set(ci, opt, val)
    cmdreply(context.bot, ci, '<option %s set to %s>' % (opt, val))
  else:
    cmdreply(context.bot, ci, '<invalid option or value>')

def cmd_option_flush(update: Update, context: CallbackContext):
  if not update.message:
    return
  options.clear()
  badword_cache.clear()
  cmdreply(context.bot, update.message.chat_id, '<done>')

def logcmd(update: Update, context: CallbackContext):
  if not update.message:
    return
  ci, fro, fron, froi = cifrofron(update)
  txt = update.message.text
  logging.info('[COMMAND] %s/%s: %s' % (fron, fro, txt))
  log_cmd(txt, conversation = update.message.chat, user = update.message.from_user)

helpstring = """Talk to me and I'll reply, or add me to a group and I'll talk once in a while. I don't talk in groups too much, unless you mention my name.
Commands:
/option_set reply_prob <value> - set my reply probability in this chat when my name is not mentioned. Defaults to 0.02 in groups. (0-1.0)
/option_set sticker_prob <value> - set the probability of sending a (often NSFW) sticker in place of an emoji. Defaults to 0 in groups.
/option_set admin_only <0|1> - when set to 1, only admins can change options and bad words
/badword bad_word - add or remove bad_word from the per channel bad word list. Lists bad words when used without an argument.
/pq - forward message to %s
/stats - print group/user stats
"""

@cmd_ratelimit
def cmd_help(update: Update, context: CallbackContext):
  cmdreply(context.bot, update.message.chat_id, helpstring % (Config.get('Telegram', 'QuoteChannel'),))

@inqueue(cmdqueue)
@cmd_ratelimit
def cmd_pq(update: Update, context: CallbackContext):
  ci, fro, fron, froi = cifrofron(update)
  msg = update.message
  if (not msg.reply_to_message) or (msg.reply_to_message.from_user.id != context.bot.id):
    cmdreply(context.bot, ci, '<send that as a reply to my message!>')
    return

  repl = msg.reply_to_message
  replid = repl.message_id

  if (repl.sticker or not repl.text):
    cmdreply(context.bot, ci, '<only regular text messages are supported>')
    return
  if (replid in pqed_messages) or (already_pqd(repl.text)):
    cmdreply(context.bot, ci, '<message already forwarded>')
    return
  if replid in command_replies:
    cmdreply(context.bot, ci, '<that is a silly thing to forward!>')
    return
  if pq_limit_check(froi) >= 5:
    cmdreply(context.bot, ci, '<slow down a little!>')
    return
  context.bot.forwardMessage(chat_id=Config.get('Telegram', 'QuoteChannel'), from_chat_id=ci, message_id=replid)
  pqed_messages.add(replid)
  log_pq(ci, froi, repl.text)

@inqueue(cmdqueue)
@cmd_ratelimit
def cmd_stats(update: Update, context: CallbackContext):
  ci, fro, fron, froi = cifrofron(update)
  recv, sent, firstdate, rank, trecv, tsent, actusr, actgrp, quality = db_stats(ci)
  quality_s = ("%.0f%%" % (quality*100)) if quality else "Unknown"
  cmdreply(context.bot, ci, 'Chat stats for %s:\nMessages received: %d (%d total)\nMessages sent: %d (%d total)\nFirst message: %s\nGroup/user rank: %d\n'
                    'Chat quality: %s\n'
                    'Users/groups active in the last 48 hours: %d/%d'
                    % (fron, recv, trecv, sent, tsent, firstdate.isoformat() if firstdate else 'Never', rank, quality_s, actusr, actgrp))

@cmd_ratelimit
def cmd_badword(update: Update, context: CallbackContext):
  ci, fro, fron, froi = cifrofron(update)
  msg = update.message.text
  msg_split = msg.split(' ', 1)
  bw = get_badwords(ci)
  if len(msg_split) == 1:
    cmdreply(context.bot, ci, '< Current bad words: %s (%d) >' % (' '.join((repr(w) for w in bw)), len(bw)))
  else:
    if not admin_check(context.bot, ci, froi):
      cmdreply(context.bot, ci, '< you are not allowed to use this command >')
      return
    badword = msg_split[1].strip().lower()
    if '\n' in badword:
      cmdreply(context.bot, ci, '< Bad word contains newline >')
      return
    if badword in bw:
      delete_badword(ci, badword)
      cmdreply(context.bot, ci, '< Bad word %s removed >' % (repr(badword)))
    else:
      add_badword(ci, badword, froi)
      cmdreply(context.bot, ci, '< Bad word %s added >' % (repr(badword)))

def thr_console():
  for line in sys.stdin:
    pass

threads.start_thread(args=(downloadqueue, 'download'))
threads.start_thread(args=(logqueue, 'dblogger'))
threads.start_thread(args=(cmdqueue, 'commands'))
threads.start_thread(target=thr_console, args=())


nn = HTTPNN(Config.get('Backend', 'Url'), Config.get('Backend', 'Keyprefix'))
nn.run_thread()
nn.loop.set_default_executor(ThreadPoolExecutor(max_workers=4))

updater = Updater(token=Config.get('Telegram','Token'), request_kwargs={'read_timeout': 10, 'connect_timeout': 15}, use_context=True)
dispatcher = updater.dispatcher

dispatcher.add_handler(CommandHandler('me', me), 0)
dispatcher.add_handler(MessageHandler(Filters.command, logcmd), 0)

dispatcher.add_handler(MessageHandler(Filters.text, msg), 1)

dispatcher.add_handler(MessageHandler(Filters.sticker, sticker), 2)
dispatcher.add_handler(MessageHandler(Filters.video, video), 2)
dispatcher.add_handler(MessageHandler(Filters.document, document), 2)
dispatcher.add_handler(MessageHandler(Filters.audio, audio), 2)
dispatcher.add_handler(MessageHandler(Filters.photo, photo), 2)
dispatcher.add_handler(MessageHandler(Filters.voice, voice), 2)
dispatcher.add_handler(MessageHandler(Filters.status_update, status), 2)

dispatcher.add_handler(CommandHandler('start', start), 3)
dispatcher.add_handler(CommandHandler('givesticker', givesticker), 3)
dispatcher.add_handler(CommandHandler('option_get', cmd_option_get), 3)
dispatcher.add_handler(CommandHandler('option_set', cmd_option_set), 3)
dispatcher.add_handler(CommandHandler('option_flush', cmd_option_flush), 3)
dispatcher.add_handler(CommandHandler('help', cmd_help), 3)
dispatcher.add_handler(CommandHandler('pq', cmd_pq), 3)
dispatcher.add_handler(CommandHandler('stats', cmd_stats), 3)
dispatcher.add_handler(CommandHandler('badword', cmd_badword), 3)
dispatcher.add_handler(CommandHandler('download_photo', cmd_download_photo), 3)

sticker_emojis = set(get_sticker_emojis())
logging.info("%d sticker emojis loaded" % len(sticker_emojis))

updater.start_polling(timeout=60, read_latency=30)
