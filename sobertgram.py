from telegram.ext import Updater, MessageHandler, Filters, CommandHandler
from telegram import ChatAction
import logging
import socket
import MySQLdb
import re
import sys
from time import time
from random import randint
import ConfigParser

Config = ConfigParser.ConfigParser()

convos = {}
times = {}
known_stickers = set()

def getconv(convid):
  if convid not in convos:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.connect((Config.get('Backend', 'Host'), Config.getint('Backend', 'Port')))
    f = s.makefile()
    convos[convid] = (s,f)
  times[convid] = time()
  return convos[convid]

def convclean():
  now = time()
  for convid in times:
    if (convid in convos) and (times[convid] + Config.getfloat('Chat', 'Timeout') * 60 * 60 < now):
      print('Deleting conversation %d' % (convid,))
      s = convos[convid][0]
      s.shutdown(socket.SHUT_RDWR)
      s.close()
      convos[convid][1].close()
      del convos[convid]

def put(convid, text):
  if text == '':
    return
  text = re.sub('[\r\n]+', '\n',text).strip("\r\n")
  try:
    (s, f) = getconv(convid)
    s.send((text + '\n').encode('utf-8'))
  except Exception as e:
    print str(e)
    del convos[convid]

def get(convid):
  try:
    (s, f) = getconv(convid)
    s.send('\n')
    return f.readline().rstrip()
  except Exception as e:
    print str(e)
    del convos[convid]
    return ''

def get_dbcon():
  db = MySQLdb.connect(host=Config.get('Database', 'Host'), user=Config.get('Database', 'User'), passwd=Config.get('Database', 'Password'), db=Config.get('Database', 'Database'), charset='utf8')
  cur = db.cursor()
  cur.execute('SET NAMES utf8mb4')
  return db, cur

def log(conv, username, sent, text):
  db, cur = get_dbcon()
  cur.execute("INSERT INTO `chat` (`convid`, `from`, `sent`, `text`) VALUES (%s, %s, %s, %s)", (conv, username, sent, text))
  db.commit()
  db.close()

def log_sticker(conv, username, sent, text, file_id, set_name):
  db, cur = get_dbcon()
  cur.execute("INSERT INTO `chat` (`convid`, `from`, `sent`, `text`) VALUES (%s, %s, %s, %s)", (conv, username, sent, text))
  cur.execute("INSERT INTO `chat_sticker` (`id`, `file_id`, `set_name`) VALUES (LAST_INSERT_ID(), %s, %s)", (file_id, set_name))
  if file_id not in known_stickers:
    cur.execute("SELECT COUNT(*) FROM `stickers` WHERE `file_id` = %s", (file_id,))
    (exists,) = cur.fetchone()
    if exists == 0:
      print "Adding sticker <%s> <%s> < %s >" %  (file_id, set_name, text)
      cur.execute("REPLACE INTO `stickers` (`file_id`, `emoji`, `set_name`) VALUES (%s, %s, %s)", (file_id, text, set_name))
  db.commit()
  db.close()
  if file_id not in known_stickers:
    known_stickers.add(file_id)

def rand_sticker():
  db, cur = get_dbcon()
  cur.execute("SELECT `file_id`, `emoji`, `set_name` FROM `stickers` ORDER BY RAND() LIMIT 1")
  row = cur.fetchone()
  db.close()
  return row

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)

def sendreply(bot, ci, fro):
  bot.sendChatAction(chat_id=ci, action=ChatAction.TYPING)
  sys.stdout.write('  => ')
  sys.stdout.flush()
  msg = get(ci)
  print(msg)
  log(ci, fro, 1, msg)
  bot.sendMessage(chat_id=ci, text=msg)

def getmessage(bot, ci, fro, txt):
  print('%s/%d: %s' % (fro, ci, txt))
  put(ci, txt)
  log(ci, fro, 0, txt)


def msg(bot, update):
  ci = update.message.chat_id
  txt = update.message.text
  fro = update.message.from_user.username
  getmessage(bot, ci, fro, txt)
  if (ci > 0) or (randint(0, 100) < 2) or (Config.get('Chat', 'Keyword') in txt.lower()):
    sendreply(bot, ci, fro)
  convclean()

def start(bot, update):
  ci = update.message.chat_id
  fro = update.message.from_user.username
  print('%s/%d /start' % (fro, ci))
  sendreply(bot, ci, fro)

def me(bot, update):
  ci = update.message.chat_id
  txt = update.message.text
  fro = update.message.from_user.username
  getmessage(bot, ci, fro, txt)
  sendreply(bot, ci, fro)

def sticker(bot, update):
  ci = update.message.chat_id
  fro = update.message.from_user.username
  st = update.message.sticker
  set = '<unnamed>' if st.set_name is None else st.set_name
  emo = st.emoji or ''
  print('%s/%d: [sticker <%s> <%s> < %s >]' % (fro, ci, st.file_id, set, emo))
  put(ci, emo)
  log_sticker(ci, fro, 0, emo, st.file_id, set)
  #bot.sendSticker(chat_id=ci, sticker=st.file_id)
  if (ci > 0) or (randint(0, 100) < 2):
    sendreply(bot, ci, fro)

def givesticker(bot, update):
  ci = update.message.chat_id
  fro = update.message.from_user.username
  fid, emo, set = rand_sticker()
  print('%s/%d: [giving random sticker: <%s> <%s>]' % (fro, ci, fid, set))
  bot.sendSticker(chat_id=ci, sticker=fid)

if len(sys.argv) != 2:
  raise Exception("Wrong number of arguments")
Config.read(sys.argv[1])

updater = Updater(token=Config.get('Telegram','Token'))
dispatcher = updater.dispatcher

dispatcher.add_handler(MessageHandler(Filters.text, msg))
dispatcher.add_handler(MessageHandler(Filters.sticker, sticker))
dispatcher.add_handler(CommandHandler('start', start))
dispatcher.add_handler(CommandHandler('me', me))
dispatcher.add_handler(CommandHandler('givesticker', givesticker))

updater.start_polling()
