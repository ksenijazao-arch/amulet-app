import asyncio, base64, datetime, hashlib, hmac, json, os, re, time, urllib.parse
import aiohttp
from aiohttp import web
HERE = os.path.dirname(os.path.abspath(__file__))
TOKEN = os.environ['BOT_TOKEN']
DBURL = os.environ['DATABASE_URL']
HOST = re.search(r'@([^/]+)/', DBURL).group(1)
KINDS = {'venus', 'lotus', 'amrita'}
DECOS = {'pearl', 'star', 'flower', 'drop', 'feather', 'moon', 'crystal', 'leaf', 'diamond'}
_S = None

def sess():
    global _S
    if _S is None or _S.closed:
        _S = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=25))
    return _S

async def q(sql, params=()):
    async with sess().post(f'https://{HOST}/sql', json={'query': sql, 'params': list(params)},
                           headers={'Neon-Connection-String': DBURL}) as r:
        j = await r.json()
        if r.status != 200:
            raise RuntimeError(j.get('message', r.status))
        return j.get('rows', [])

async def init_db():
    await q('create schema if not exists amulet')
    await q('create table if not exists amulet.users(tg_id bigint primary key, kind text, created text)')
    await q('create table if not exists amulet.marks(tg_id bigint, day text, morning integer default 0, evening integer default 0, deco text, primary key(tg_id,day))')

def today():
    return (datetime.datetime.utcnow() + datetime.timedelta(hours=3)).strftime('%Y-%m-%d')

def check(init):
    p = dict(urllib.parse.parse_qsl(init, keep_blank_values=True))
    h = p.pop('hash', None)
    if not h:
        return None
    s = '\n'.join(f'{k}={v}' for k, v in sorted(p.items()))
    key = hmac.new(b'WebAppData', TOKEN.encode(), hashlib.sha256).digest()
    if not hmac.compare_digest(hmac.new(key, s.encode(), hashlib.sha256).hexdigest(), h):
        return None
    if time.time() - int(p.get('auth_date', '0')) > 86400 * 3:
        return None
    return json.loads(p['user'])['id']

async def state(uid):
    u, rows = await asyncio.gather(
        q('select kind from amulet.users where tg_id=$1::bigint', [str(uid)]),
        q('select day,morning,evening,deco from amulet.marks where tg_id=$1::bigint order by day', [str(uid)]))
    done = [r for r in rows if r['morning'] or r['evening']]
    t = next((r for r in rows if r['day'] == today()), None)
    return {'decos': [(r['deco'] or 'pearl') for r in done], 'seed': uid % 1000003,
            'tdeco': (t['deco'] if t and t['deco'] else None), 'days': [r['day'] for r in done],
            'kind': u[0]['kind'] if u else None, 'steps': min(40, len(done)),
            'pearls': sum(1 for r in rows if r['morning'] and r['evening']),
            'morning': bool(t and t['morning']), 'evening': bool(t and t['evening'])}

async def api(req):
    uid = check(req.headers.get('X-Init-Data', ''))
    if not uid:
        return web.json_response({'error': 'auth'}, status=401)
    act = req.match_info['act']
    try:
        if act == 'save':
            try:
                d = (await req.json()).get('img', '')
                raw = base64.b64decode(d.split(',', 1)[1]) if ',' in d else b''
            except Exception:
                return web.json_response({'error': 'img'}, status=400)
            if not (5000 < len(raw) < 6_000_000) or raw[:2] != b'\xff\xd8':
                return web.json_response({'error': 'img'}, status=400)
            st = await state(uid)
            fd = aiohttp.FormData()
            fd.add_field('chat_id', str(uid))
            fd.add_field('caption', '🌙 Твой амулет пробуждения · %d из 40\nМожно сохранить на телефон и переслать.' % st['steps'])
            fd.add_field('photo', raw, filename='amulet.jpg', content_type='image/jpeg')
            async with sess().post(f'https://api.telegram.org/bot{TOKEN}/sendPhoto', data=fd) as r:
                j = await r.json()
            return web.json_response({'ok': bool(j.get('ok')), 'error': None if j.get('ok') else 'start'})
        if act == 'choose':
            k = (await req.json()).get('kind')
            if k not in KINDS:
                return web.json_response({'error': 'kind'}, status=400)
            await q('insert into amulet.users(tg_id,kind,created) values($1::bigint,$2,$3) on conflict(tg_id) do update set kind=excluded.kind', [str(uid), k, today()])
        elif act == 'mark':
            body = await req.json()
            w = body.get('when')
            if w not in ('morning', 'evening'):
                return web.json_response({'error': 'when'}, status=400)
            if not await q('select 1 as x from amulet.users where tg_id=$1::bigint', [str(uid)]):
                return web.json_response({'error': 'choose first'}, status=400)
            d = body.get('deco') if body.get('deco') in DECOS else None
            await q('insert into amulet.marks(tg_id,day,morning,evening,deco) values($1::bigint,$2,$3,$4,$5) '
                    'on conflict(tg_id,day) do update set morning=greatest(amulet.marks.morning,excluded.morning), '
                    'evening=greatest(amulet.marks.evening,excluded.evening), deco=coalesce(amulet.marks.deco,excluded.deco)',
                    [str(uid), today(), 1 if w == 'morning' else 0, 1 if w == 'evening' else 0, d])
        elif act != 'state':
            return web.json_response({'error': 'act'}, status=404)
        return web.json_response(await state(uid))
    except Exception as e:
        print('ERR', act, e, flush=True)
        return web.json_response({'error': 'server'}, status=500)

async def index(req):
    return web.FileResponse(os.path.join(HERE, 'static', 'index.html'), headers={'Cache-Control': 'no-store'})

async def health(req):
    return web.Response(text='ok')

async def on_start(app):
    await init_db()

app = web.Application(client_max_size=8 * 1024 * 1024)
app.on_startup.append(on_start)
app.router.add_get('/', index)
app.router.add_get('/health', health)
app.router.add_route('*', '/api/{act}', api)
app.router.add_static('/s/', os.path.join(HERE, 'static'))
if __name__ == '__main__':
    web.run_app(app, host='0.0.0.0', port=int(os.environ.get('PORT', '10000')), print=None)
