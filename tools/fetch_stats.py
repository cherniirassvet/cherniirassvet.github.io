#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Сборщик данных для сайта «Чёрный Рассвет».

Что делает:
  1. Спрашивает у игрового сервера онлайн и карту по протоколу A2S (UDP).
  2. Забирает через API панели файлы csstats.dat и zm_clans.ini.
  3. Кладёт разобранные данные в data/online.json и data/top.json.

Запускается роботом GitHub Actions. Ключ панели берётся из переменной
окружения PANEL_KEY (в репозитории это секрет, в коде его нет).
Только стандартная библиотека Python — ставить ничего не нужно.
"""

import json
import os
import re
import socket
import struct
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

# ----------------------------------------------------------------- настройки

SERVER_IP    = os.environ.get('SERVER_IP',   '195.60.166.224')
SERVER_PORT  = int(os.environ.get('SERVER_PORT', '27374'))
SERVER_SLOTS = int(os.environ.get('SERVER_SLOTS', '32'))

PANEL_URL    = os.environ.get('PANEL_URL',    'https://bropanel.gamehostbros.com').rstrip('/')
PANEL_SERVER = os.environ.get('PANEL_SERVER', 'c06af452')
PANEL_KEY    = os.environ.get('PANEL_KEY',    '').strip()

# Куда класть результат
OUT_DIR = os.environ.get('OUT_DIR', 'data')

# Где искать файлы статистики. Если не найдены — робот обойдёт папки сам.
CSSTATS_PATHS = [
    '/cstrike/addons/amxmodx/data/csstats.dat',
    '/addons/amxmodx/data/csstats.dat',
    '/game/cstrike/addons/amxmodx/data/csstats.dat',
]
CLANS_PATHS = [
    '/cstrike/addons/amxmodx/configs/zm_clans.ini',
    '/cstrike/addons/amxmodx/data/zm_clans.ini',
    '/cstrike/addons/amxmodx/configs/zombie_plague/zm_clans.ini',
    '/addons/amxmodx/configs/zm_clans.ini',
]

TOP_KEEP   = 50     # сколько игроков отдавать сайту (он показывает 15, но сортирует сам)
CLANS_KEEP = 30

def log(msg):
    print(msg, flush=True)

# --------------------------------------------------------------- A2S по UDP

HDR_SIMPLE = b'\xFF\xFF\xFF\xFF'
HDR_SPLIT  = b'\xFF\xFF\xFF\xFE'


def txt(raw):
    """Ники приходят в CP1251. Если не разбирается — читаем как UTF-8."""
    raw = raw.split(b'\x00')[0]
    for enc in ('utf-8', 'cp1251'):
        try:
            return raw.decode(enc).strip()
        except UnicodeDecodeError:
            continue
    return raw.decode('cp1251', 'replace').strip()


def dec(raw):
    """Целый файл: сначала UTF-8, если не разбирается — CP1251."""
    for enc in ('utf-8', 'cp1251'):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode('cp1251', 'replace')


class Reader(object):
    """Пошаговое чтение ответа сервера."""

    def __init__(self, data):
        self.d = data
        self.i = 0

    def byte(self):
        v = self.d[self.i]
        self.i += 1
        return v

    def short(self):
        v = struct.unpack_from('<h', self.d, self.i)[0]
        self.i += 2
        return v

    def long(self):
        v = struct.unpack_from('<i', self.d, self.i)[0]
        self.i += 4
        return v

    def flt(self):
        v = struct.unpack_from('<f', self.d, self.i)[0]
        self.i += 4
        return v

    def string(self):
        end = self.d.index(b'\x00', self.i)
        v = txt(self.d[self.i:end])
        self.i = end + 1
        return v


def recv_full(sock):
    """Читает ответ. Разбитый на пакеты — собирает по порядку."""
    data, _ = sock.recvfrom(8192)
    if data[:4] == HDR_SIMPLE:
        return data[4:]
    if data[:4] != HDR_SPLIT:
        return None

    parts, total = {}, None
    while True:
        marker = data[8]                    # младшие 4 бита — сколько пакетов,
        total = marker & 0x0F               # старшие 4 бита — номер этого
        parts[(marker >> 4) & 0x0F] = data[9:]
        if total and len(parts) >= total:
            break
        data, _ = sock.recvfrom(8192)
        if data[:4] != HDR_SPLIT:
            break
    body = b''.join(parts[k] for k in sorted(parts))
    return body[4:] if body[:4] == HDR_SIMPLE else body


def ask(sock, addr, payload, tries=2):
    for _ in range(tries):
        try:
            sock.sendto(payload, addr)
            return recv_full(sock)
        except (socket.timeout, OSError):
            continue
    return None


def a2s_info(ip, port, timeout=5.0):
    """Онлайн, карта, количество слотов."""
    addr = (ip, port)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(timeout)
    try:
        req = HDR_SIMPLE + b'TSource Engine Query\x00'
        body = ask(sock, addr, req)
        if body and body[:1] == b'A':                 # сервер просит челлендж
            body = ask(sock, addr, req + body[1:5])
        if not body:
            return None

        kind, r = body[:1], Reader(body[1:])
        if kind == b'I':                              # современный ответ
            r.byte()
            name, mp = r.string(), r.string()
            r.string(); r.string()
            r.short()
            players, maxp, bots = r.byte(), r.byte(), r.byte()
        elif kind == b'm':                            # старый GoldSrc
            r.string()
            name, mp = r.string(), r.string()
            r.string(); r.string()
            players, maxp, bots = r.byte(), r.byte(), 0
        else:
            return None
        return {'name': name, 'map': mp, 'players': players, 'max': maxp, 'bots': bots}
    except Exception as e:
        log('  A2S_INFO не разобран: %s' % e)
        return None
    finally:
        sock.close()


def a2s_players(ip, port, timeout=5.0):
    """Список ников. Боты YaPB приходят наравне с людьми и никак не помечены."""
    addr = (ip, port)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(timeout)
    try:
        body = ask(sock, addr, HDR_SIMPLE + b'U' + b'\xFF\xFF\xFF\xFF')
        if body and body[:1] == b'A':
            body = ask(sock, addr, HDR_SIMPLE + b'U' + body[1:5])
        if not body or body[:1] != b'D':
            return []

        r = Reader(body[1:])
        count, out = r.byte(), []
        for _ in range(count):
            try:
                r.byte()
                nick = r.string()
                score = r.long()
                r.flt()
            except (IndexError, struct.error, ValueError):
                break
            if nick:
                out.append({'name': nick, 'score': score})
        return out
    except Exception as e:
        log('  A2S_PLAYERS не разобран: %s' % e)
        return []
    finally:
        sock.close()

# ------------------------------------------------------------- API панели

def panel_request(url, timeout=30):
    req = urllib.request.Request(url, headers={
        'Authorization': 'Bearer ' + PANEL_KEY,
        'Accept': 'application/json',
        'User-Agent': 'blackdawn-stats/1.0',
    })
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def panel_file(path):
    """Скачивает файл с сервера через панель. Возвращает bytes или None."""
    url = '%s/api/client/servers/%s/files/contents?file=%s' % (
        PANEL_URL, PANEL_SERVER, urllib.parse.quote(path, safe=''))
    try:
        return panel_request(url)
    except urllib.error.HTTPError as e:
        if e.code not in (404, 400):
            log('  панель ответила %s на %s' % (e.code, path))
        return None
    except Exception as e:
        log('  панель недоступна (%s)' % e)
        return None


def panel_list(directory):
    """Содержимое папки на сервере: [(имя, это_папка), ...]"""
    url = '%s/api/client/servers/%s/files/list?directory=%s' % (
        PANEL_URL, PANEL_SERVER, urllib.parse.quote(directory, safe=''))
    try:
        data = json.loads(panel_request(url, timeout=20).decode('utf-8', 'replace'))
    except Exception:
        return []
    out = []
    for item in data.get('data', []):
        a = item.get('attributes', {})
        out.append((a.get('name', ''), bool(a.get('is_file')) is False))
    return out


def panel_find(filename, roots=('/',), max_dirs=80):
    """Обходит папки вширь и ищет файл по имени. Возвращает путь или None."""
    seen, queue = set(), list(roots)
    while queue and len(seen) < max_dirs:
        d = queue.pop(0)
        if d in seen:
            continue
        seen.add(d)
        for name, is_dir in panel_list(d):
            full = (d.rstrip('/') + '/' + name)
            if not is_dir and name == filename:
                return full
            if is_dir and not name.startswith('.'):
                queue.append(full)
    return None


def grab(filename, candidates):
    """Пробует известные пути, потом ищет файл сам."""
    for p in candidates:
        raw = panel_file(p)
        if raw:
            log('  %s найден: %s (%d байт)' % (filename, p, len(raw)))
            return raw
    log('  %s по обычным путям не найден, обхожу папки...' % filename)
    found = panel_find(filename)
    if found:
        raw = panel_file(found)
        if raw:
            log('  %s найден: %s (%d байт)' % (filename, found, len(raw)))
            return raw
    log('  %s не найден на сервере' % filename)
    return None

# -------------------------------------------------------- разбор csstats.dat

def _csstats_from(raw, off):
    """Одна попытка разбора с заданного смещения."""
    i, n, out = off, len(raw), []
    while i + 2 <= n:
        ln = struct.unpack_from('<h', raw, i)[0]
        i += 2
        if ln <= 0 or ln > 128 or i + ln > n:
            break
        name = txt(raw[i:i + ln])
        i += ln
        if i + 2 > n:
            break
        ls = struct.unpack_from('<h', raw, i)[0]
        i += 2
        if ls < 0 or ls > 128 or i + ls > n:
            break
        steam = txt(raw[i:i + ls])
        i += ls
        if i + 80 > n:
            break
        v = struct.unpack_from('<20i', raw, i)
        i += 80
        if not name:
            continue
        # 0 tks, 1 урон, 2 смерти, 3 убийства, 4 выстрелы, 5 попадания, 6 в голову
        out.append({
            'name': name, 'steam': steam,
            'damage': max(0, v[1]), 'deaths': max(0, v[2]),
            'kills': max(0, v[3]),  'hs': max(0, v[6]),
        })
    return out, i


def parse_csstats(raw):
    """Формат: short версия, дальше по игроку — ник, SteamID и 20 чисел.

    Смещение начала подбирается: у разных сборок AMXX перед списком может
    стоять лишнее поле. Верным считается разбор, который дочитал файл до конца.
    """
    best, best_score = [], -1
    for off in (2, 6, 4, 0, 8):
        try:
            rows, consumed = _csstats_from(raw, off)
        except Exception:
            continue
        if not rows:
            continue
        tail = len(raw) - consumed
        score = len(rows) * 1000 - tail          # чем меньше хвост, тем лучше
        if tail <= 8 and score > best_score:
            best, best_score = rows, score
    if not best:
        log('  csstats.dat: разобрать не удалось')
    else:
        log('  csstats.dat: игроков %d' % len(best))
    return best

# ------------------------------------------------------ разбор zm_clans.ini

def parse_clans(text, nick_by_steam=None):
    """Строка: название тег уровень опыт монеты банк слоты STEAM_лидера победы поражения ...

    Название может содержать пробелы, поэтому опираемся на SteamID лидера:
    от него отсчитываем пять чисел назад, перед ними стоит тег.
    """
    nick_by_steam = nick_by_steam or {}
    out = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line[0] in ';#[/':
            continue
        tok = line.split()
        k = next((j for j, t in enumerate(tok) if t.upper().startswith('STEAM_')), -1)
        if k < 6:
            continue
        try:
            level, exp, coins, bank, slots = (int(tok[k - 5]), int(tok[k - 4]),
                                              int(tok[k - 3]), int(tok[k - 2]),
                                              int(tok[k - 1]))
        except ValueError:
            continue

        def num(j):
            try:
                return int(tok[j])
            except (IndexError, ValueError):
                return 0

        tag = tok[k - 6].strip('"')
        name = ' '.join(tok[:k - 6]).strip().strip('"')
        if not name:
            name = tag
        out.append({
            'name': name, 'tag': tag, 'level': level, 'exp': exp,
            'coins': coins, 'bank': bank, 'slots': slots,
            'wins': num(k + 1), 'losses': num(k + 2),
            'leader': nick_by_steam.get(tok[k], ''),
        })
    log('  zm_clans.ini: кланов %d' % len(out))
    if not out:
        for line in [l.strip() for l in text.splitlines() if l.strip()][:4]:
            safe = re.sub(r'STEAM_[0-9:]+', 'STEAM_x:y:z', line)
            log('    не разобрана строка: ' + safe[:200])
    return out

# ------------------------------------------------------------------ запись

def write_json(name, obj):
    os.makedirs(OUT_DIR, exist_ok=True)
    path = os.path.join(OUT_DIR, name)
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(obj, f, ensure_ascii=False, separators=(',', ':'))
        f.write('\n')
    os.replace(tmp, path)
    log('  записан %s (%d байт)' % (path, os.path.getsize(path)))


def main():
    now = int(time.time())
    log('=== Чёрный Рассвет: сбор данных ===')
    log('Сервер %s:%d' % (SERVER_IP, SERVER_PORT))

    # 1. Онлайн
    log('[1/3] Спрашиваю сервер по A2S...')
    info = a2s_info(SERVER_IP, SERVER_PORT)
    if info:
        players = a2s_players(SERVER_IP, SERVER_PORT)
        log('  ответил: карта %s, игроков %d из %d' % (info['map'], info['players'], info['max']))
        write_json('online.json', {
            'online':  True,
            'players': info['players'],
            'max':     info['max'] or SERVER_SLOTS,
            'map':     info['map'],
            'list':    players,
            'updated': now,
        })
    else:
        log('  сервер не ответил')
        write_json('online.json', {
            'online': False, 'players': 0, 'max': SERVER_SLOTS,
            'map': '', 'list': [], 'updated': now,
        })

    # 2. Топы через панель
    if not PANEL_KEY:
        log('[2/3] Ключа панели нет — топы пропускаю, старый data/top.json остаётся как был.')
        log('=== готово (без топов) ===')
        return 0

    log('[2/3] Забираю файлы статистики через панель...')
    stats_raw = grab('csstats.dat',  CSSTATS_PATHS)
    clans_raw = grab('zm_clans.ini', CLANS_PATHS)

    log('[3/3] Разбираю...')
    people = parse_csstats(stats_raw) if stats_raw else []
    nick_by_steam = {p['steam']: p['name'] for p in people if p.get('steam')}
    clans = parse_clans(dec(clans_raw), nick_by_steam) if clans_raw else []

    if not people and not clans:
        log('  данных нет — старый data/top.json не трогаю')
        log('=== готово (топы не обновились) ===')
        return 0

    # Сайт сортирует сам по убийствам или урону, поэтому отдаём обе выборки
    by_kills  = sorted(people, key=lambda p: (-p['kills'],  -p['damage']))[:TOP_KEEP]
    by_damage = sorted(people, key=lambda p: (-p['damage'], -p['kills']))[:TOP_KEEP]
    seen, top_players = set(), []
    for p in by_kills + by_damage:
        key = (p['name'], p['steam'])
        if key in seen:
            continue
        seen.add(key)
        top_players.append({'name': p['name'], 'kills': p['kills'],
                            'damage': p['damage'], 'hs': p['hs'], 'deaths': p['deaths']})

    top_clans = sorted(clans, key=lambda c: (-c['exp'], -c['level']))[:CLANS_KEEP]

    write_json('top.json', {
        'players': top_players,
        'clans':   [{'name': c['name'], 'tag': c['tag'], 'level': c['level'],
                     'exp': c['exp'], 'wins': c['wins'], 'slots': c['slots'],
                     'leader': c['leader']} for c in top_clans],
        'updated': now,
    })
    log('=== готово: игроков %d, кланов %d ===' % (len(top_players), len(top_clans)))
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception as exc:
        log('СБОЙ: %s' % exc)
        sys.exit(0)      # не роняем робота — пусть попробует через 15 минут
