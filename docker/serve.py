#!/usr/bin/env python3
"""
ZeroCosts — local server.

Serves the course and lets the page compile and run your code with the Rust
toolchain installed on THIS machine (rustc, cargo, rustfmt, clippy) and, if
present, GHC for the Haskell chapter.

    python serve.py            # then open http://127.0.0.1:8765
    python serve.py --open     # opens your browser for you
    python serve.py --port 9000 --workspace D:/code/zerocosts

Safety: the server listens on 127.0.0.1 only, checks the Host header, and every
API call must carry a random token that is generated at start-up and handed
only to pages it serves itself. Other websites you visit cannot use it.
Your code runs with YOUR user's permissions — exactly like `cargo run` in a
terminal — so only run code you understand.
Standard library only: no pip installs needed. Python 3.8+.
"""
import argparse, json, os, platform, re, secrets, shlex, shutil, subprocess, sys, tempfile, threading, time, webbrowser
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse, unquote
import urllib.request, urllib.error

HERE = Path(__file__).resolve().parent
WEB = (HERE / 'web').resolve()
IS_WIN = os.name == 'nt'
EXE = '.exe' if IS_WIN else ''
OUT_CAP = 256 * 1024
PUBLIC_PORT = 0                      # port the browser uses to reach us (differs from the bind port only behind Docker's port mapping)
IN_CONTAINER = os.environ.get('RF_CONTAINER') == '1'   # set by the official Docker image
TOKEN = secrets.token_urlsafe(24)   # per-run in standalone mode; persistent pairing code with --site
TOKENFILE = Path(os.environ.get('XDG_CONFIG_HOME', str(Path.home() / '.config'))) / 'rustforge' / 'helper_token'
MIME = {'.html': 'text/html; charset=utf-8', '.js': 'text/javascript; charset=utf-8', '.json': 'application/json; charset=utf-8',
        '.css': 'text/css; charset=utf-8', '.svg': 'image/svg+xml', '.png': 'image/png', '.ico': 'image/x-icon', '.txt': 'text/plain; charset=utf-8'}

# crates the course uses, with sensible versions/features; anything else gets "*"
KNOWN_CRATES = {
    'rand': '"0.9"', 'serde': '{ version = "1", features = ["derive"] }', 'serde_json': '"1"', 'tokio': '{ version = "1", features = ["full"] }',
    'rayon': '"1"', 'regex': '"1"', 'anyhow': '"1"', 'thiserror': '"2"', 'clap': '{ version = "4", features = ["derive"] }',
    'crossbeam': '"0.8"', 'crossbeam-channel': '"0.5"', 'crossbeam-utils': '"0.8"', 'bytes': '"1"', 'itertools': '"0.14"',
    'once_cell': '"1"', 'parking_lot': '"0.12"', 'memchr': '"2"', 'memmap2': '"0.9"', 'ahash': '"0.8"', 'rustc-hash': '"2"',
    'futures': '"0.3"', 'chrono': '"0.4"', 'colored': '"3"', 'csv': '"1"', 'smallvec': '"1"', 'bumpalo': '"3"',
    'tracing': '"0.1"', 'tracing-subscriber': '"0.3"', 'mio': '{ version = "1", features = ["os-poll", "net"] }', 'socket2': '"0.5"',
    'libc': '"0.2"', 'num_cpus': '"1"', 'hashbrown': '"0.15"', 'arrayvec': '"0.7"', 'indexmap': '"2"', 'criterion': '"0.5"',
}
NOT_CRATES = {'std', 'core', 'alloc', 'crate', 'self', 'super', 'proc_macro', 'test',
              'i8', 'i16', 'i32', 'i64', 'i128', 'isize', 'u8', 'u16', 'u32', 'u64', 'u128', 'usize', 'f32', 'f64', 'bool', 'char', 'str'}


def which(*names):
    for n in names:
        p = shutil.which(n)
        if p:
            return p
    # rustup's default location, in case PATH wasn't refreshed after install
    for n in names:
        cand = Path.home() / '.cargo' / 'bin' / (n + EXE)
        if cand.exists():
            return str(cand)
    return None


def run_proc(args, cwd=None, stdin='', timeout=10.0, env=None):
    """Run a process with a hard timeout that kills the whole process tree."""
    t0 = time.perf_counter()
    kw = dict(cwd=cwd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
    if IS_WIN:
        kw['creationflags'] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kw['start_new_session'] = True
    try:
        p = subprocess.Popen(args, **kw)
    except FileNotFoundError as e:
        return {'code': -1, 'stdout': '', 'stderr': f'could not start {args[0]}: {e}', 'timed_out': False, 'ms': 0}
    timed_out = False
    try:
        out, err = p.communicate((stdin or '').encode(), timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        kill_tree(p)
        out, err = p.communicate()
    ms = round((time.perf_counter() - t0) * 1000, 1)
    dec = lambda b: b[:OUT_CAP].decode('utf-8', 'replace') + ('\n… (output truncated)' if len(b) > OUT_CAP else '')
    return {'code': p.returncode, 'stdout': dec(out), 'stderr': dec(err), 'timed_out': timed_out, 'ms': ms}


def kill_tree(p):
    try:
        if IS_WIN:
            subprocess.run(['taskkill', '/F', '/T', '/PID', str(p.pid)], capture_output=True)
        else:
            import signal
            os.killpg(p.pid, signal.SIGKILL)
    except Exception:
        try:
            p.kill()
        except Exception:
            pass


class Toolchain:
    def __init__(self):
        self.rustc = which('rustc')
        self.cargo = which('cargo')
        self.rustfmt = which('rustfmt')
        self.clippy = which('clippy-driver')
        self.runghc = which('runghc', 'runhaskell')
        self.rustfilt = which('rustfilt')
        self.gdb = which('gdb')
        self.readelf = which('readelf', 'llvm-readelf')
        self.info = {}
        self.refresh()

    def ver(self, exe, flag='--version'):
        if not exe:
            return None
        r = run_proc([exe, flag], timeout=20)
        return (r['stdout'] or r['stderr']).strip().splitlines()[0] if r['code'] == 0 and (r['stdout'] or r['stderr']).strip() else None

    def refresh(self):
        i = {'rustc': self.ver(self.rustc), 'cargo': self.ver(self.cargo), 'rustfmt': self.ver(self.rustfmt),
             'clippy': self.ver(self.clippy), 'ghc': self.ver(self.runghc), 'os': platform.system(), 'arch': platform.machine(), 'container': IN_CONTAINER,
             'gdb': self.ver(self.gdb) if self.gdb and self.readelf and platform.system() == 'Linux' else None}
        self.minor = 0
        self.sysroot = None
        if i['rustc']:
            self.sysroot = run_proc([self.rustc, '--print', 'sysroot'], timeout=20)['stdout'].strip() or None
            m = re.search(r'rustc 1\.(\d+)', i['rustc'])
            self.minor = int(m.group(1)) if m else 0
            vv = run_proc([self.rustc, '-vV'], timeout=20)['stdout']
            m = re.search(r'host: (\S+)', vv)
            i['host'] = m.group(1) if m else None
        self.edition = '2024' if self.minor >= 85 else '2021'
        i['edition'] = self.edition
        self.info = i


TC = None
WS = None
SITE = ''          # hosted-site origin allowed to talk to this helper (set with --site)


SCRATCH_LOCK = threading.Lock()
PROJ_LOCKS = {}


# ---------------------------------------------------------------- diagnostics
def parse_diags(stderr_json):
    """rustc --error-format=json → (rendered text, [diag dicts], leftover stderr)."""
    diags, rendered, other = [], [], []
    for line in stderr_json.splitlines():
        s = line.strip()
        if not s.startswith('{'):
            if s:
                other.append(line)
            continue
        try:
            d = json.loads(s)
        except ValueError:
            other.append(line)
            continue
        if d.get('$message_type', 'diagnostic') != 'diagnostic':
            continue
        add_diag(d, diags, rendered)
    return '\n\n'.join(rendered).strip(), diags, '\n'.join(other)


def add_diag(d, diags, rendered):
    if d.get('rendered'):
        rendered.append(d['rendered'].rstrip())
    user = lambda sp: (sp.get('file_name') or '').replace('\\', '/') in ('main.rs', 'src/main.rs')
    spans = d.get('spans', [])
    prim = next((sp for sp in spans if sp.get('is_primary') and user(sp)), None) or next((sp for sp in spans if user(sp)), None)
    if d.get('level') in ('error', 'warning') and not (d.get('message', '').startswith('aborting due to') or re.match(r'\d+ warnings? emitted', d.get('message', ''))):
        diags.append({'level': d.get('level'), 'code': (d.get('code') or {}).get('code'), 'message': d.get('message'),
                      'line': prim and prim.get('line_start'), 'col': prim and prim.get('column_start'),
                      'file': prim and prim.get('file_name')})


def detect_crates(code):
    mods = set(re.findall(r'^\s*(?:pub\s+)?mod\s+([a-z_][a-z0-9_]*)', code, re.M))
    found = set()
    for m in re.finditer(r'(?<![\w:])(?:::)?([a-z_][a-z0-9_]*)::', code):
        found.add(m.group(1))
    for m in re.finditer(r'^\s*(?:pub\s+)?use\s+(?:::)?([a-z_][a-z0-9_]*)\s*(?:;|\{)', code, re.M):
        found.add(m.group(1))
    for m in re.finditer(r'^\s*extern\s+crate\s+([a-z_][a-z0-9_]*)', code, re.M):
        found.add(m.group(1))
    out = []
    for c in sorted(found - NOT_CRATES - mods):
        # only treat as a crate if it is known, or it is clearly an import (use x::...)
        dashed = c.replace('_', '-')
        if c in KNOWN_CRATES or dashed in KNOWN_CRATES or re.search(r'^\s*(?:pub\s+)?use\s+(?:::)?' + c + r'\b', code, re.M) or re.search(r'#\[\s*' + c + r'::', code):
            out.append(c if c in KNOWN_CRATES else dashed if dashed in KNOWN_CRATES else c)
    return out


class _V0:
    BASIC = {'a':'i8','b':'bool','c':'char','d':'f64','e':'str','f':'f32','h':'u8','i':'isize','j':'usize','l':'i32','m':'u32','n':'i128','o':'u128','s':'i16','t':'u16','u':'()','v':'...','x':'i64','y':'u64','z':'!','p':'_'}
    def __init__(self, s):
        self.s, self.i, self.depth = s, 0, 0
    def peek(self):
        return self.s[self.i] if self.i < len(self.s) else ''
    def eat(self, c):
        if self.peek() == c:
            self.i += 1
            return True
        return False
    def nxt(self):
        c = self.peek(); self.i += 1; return c
    def b62(self):
        if self.eat('_'):
            return 0
        n = 0
        while True:
            c = self.nxt()
            if c == '_':
                return n + 1
            if not c:
                raise ValueError
            n = n * 62 + '0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ'.index(c)
    def dis(self):
        return self.b62() if self.eat('s') else 0
    def ident(self):
        self.dis()
        self.eat('u')
        j = self.i
        while self.peek().isdigit():
            self.i += 1
        n = int(self.s[j:self.i]); self.eat('_')
        r = self.s[self.i:self.i + n]; self.i += n
        return r
    def backref(self, f):
        pos = self.b62()
        save = self.i; self.i = pos; self.depth += 1
        if self.depth > 40:
            raise ValueError
        r = f(); self.i = save; self.depth -= 1
        return r
    def path(self):
        c = self.nxt()
        if c == 'C':
            return self.ident()
        if c == 'N':
            ns = self.nxt(); p = self.path(); name = self.ident()
            if ns == 'C':
                return f'{p}::{{closure}}'
            if ns == 'S':
                return f'{p}::{{shim:{name}}}' if name else f'{p}::{{shim}}'
            return f'{p}::{name}' if name else p
        if c == 'M':
            self.dis(); self.path(); return '<' + self.type() + '>'
        if c == 'X':
            self.dis(); self.path(); t = self.type(); tr = self.path(); return f'<{t} as {tr}>'
        if c == 'Y':
            t = self.type(); tr = self.path(); return f'<{t} as {tr}>'
        if c == 'I':
            p = self.path(); args = []
            while not self.eat('E'):
                a = self.garg()
                if a is not None:
                    args.append(a)
            return f'{p}::<{", ".join(args)}>' if args else p
        if c == 'B':
            return self.backref(self.path)
        raise ValueError(c)
    def garg(self):
        if self.eat('L'):
            self.b62(); return None
        if self.eat('K'):
            return self.const()
        return self.type()
    def const(self):
        if self.eat('B'):
            return self.backref(self.const)
        if self.eat('p'):
            return '_'
        t = self.nxt()
        neg = self.eat('n')
        j = self.i
        while self.peek() != '_':
            self.i += 1
        h = self.s[j:self.i]; self.i += 1
        v = int(h, 16) if h else 0
        if t == 'b':
            return 'true' if v else 'false'
        return ('-' if neg else '') + str(v)
    def binder(self):
        if self.eat('G'):
            self.b62()
    def type(self):
        c = self.peek()
        if c in self.BASIC:
            self.i += 1; return self.BASIC[c]
        self.i += 1
        if c == 'R' or c == 'Q':
            if self.eat('L'):
                self.b62()
            return ('&' if c == 'R' else '&mut ') + self.type()
        if c == 'P':
            return '*const ' + self.type()
        if c == 'O':
            return '*mut ' + self.type()
        if c == 'A':
            t = self.type(); n = self.const(); return f'[{t}; {n}]'
        if c == 'S':
            return f'[{self.type()}]'
        if c == 'T':
            ts = []
            while not self.eat('E'):
                ts.append(self.type())
            return '(' + ', '.join(ts) + (',)' if len(ts) == 1 else ')')
        if c == 'F':
            self.binder(); self.eat('U')
            if self.eat('K'):
                if not self.eat('C'):
                    self.ident()
            ps = []
            while not self.eat('E'):
                ps.append(self.type())
            r = self.type()
            return f'fn({", ".join(ps)})' + ('' if r == '()' else f' -> {r}')
        if c == 'D':
            self.binder(); bs = []
            while not self.eat('E'):
                p = self.path()
                while self.eat('p'):
                    self.ident(); self.type()
                bs.append(p)
            if self.eat('L'):
                self.b62()
            return 'dyn ' + ' + '.join(bs)
        if c == 'B':
            return self.backref(self.type)
        self.i -= 1
        return self.path()

def demangle_v0(sym):
    s = sym.lstrip('_')
    if not s.startswith('R'):
        return sym
    s = s[1:]
    if s[:1].isdigit():
        s = s[1:]  # encoding version
    try:
        d = _V0(s); r = d.path()
        return r
    except Exception:
        return sym


def demangle(text):
    if TC.rustfilt:
        r = run_proc([TC.rustfilt], stdin=text, timeout=20)
        if r['code'] == 0:
            return r['stdout']
    subs = [('$LT$', '<'), ('$GT$', '>'), ('$u20$', ' '), ('$RF$', '&'), ('$BP$', '*'), ('$C$', ','), ('$u7b$', '{'), ('$u7d$', '}'),
            ('$u5b$', '['), ('$u5d$', ']'), ('$u27$', "'"), ('$u3b$', ';'), ('$u2b$', '+'), ('$u22$', '"'), ('$LP$', '('), ('$RP$', ')')]

    def one(m):
        s, i, parts = m.group(1), 0, []
        while i < len(s) and s[i].isdigit():
            j = i
            while j < len(s) and s[j].isdigit():
                j += 1
            n = int(s[i:j])
            parts.append(s[j:j + n])
            i = j + n
        if parts and re.fullmatch(r'h[0-9a-f]{16}', parts[-1]):
            parts.pop()
        name = '::'.join(parts)
        for a, b in subs:
            name = name.replace(a, b)
        return name.replace('..', '::').replace('_$', '$')
    text = re.sub(r'(?<![\w.$])_{1,2}R[0-9A-Za-z_]+', lambda m: demangle_v0(m.group(0)), text)
    return re.sub(r'_{1,2}ZN((?:\d+[\w$.]+?)+)E', one, text)


def clean_asm(asm):
    """Keep labels and instructions of user functions; drop assembler directives."""
    funcs, cur, buf = [], None, []
    for line in asm.splitlines():
        m = re.match(r'^"?([A-Za-z_$][\w$.@]*)"?:\s*(#.*)?$', line)
        if m and not m.group(1).startswith(('.L', 'Ltmp', 'Lfunc', 'LBB', 'GCC_except', 'DW.ref', 'Lanon', 'Lexception', 'Lcst')):
            if cur and buf:
                funcs.append([cur, buf])
            cur, buf = m.group(1), []
            continue
        if cur is None:
            continue
        st = line.strip()
        if not st or st.startswith(('.cfi', '.file', '.loc', '.p2align', '.type', '.size', '.section', '.globl', '.text', '.ident', '.addrsig',
                                     '.def', '.scl', '.endef', '.seh', '.build_version', '.subsections', '.align', '.byte', '.uleb', '.long',
                                     '.quad', '.asciz', '.ascii', '.zero', '.p2', '.hidden', '.weak', '.short', '.word', '.4byte', '.8byte', '#', ';', '//')):
            continue
        if re.match(r'^\.?L(tmp|func_end|cst|exception|CPI)', st):
            continue
        buf.append(line.replace('\t', '    ').rstrip())
    if cur and buf:
        funcs.append([cur, buf])
    blob = demangle('\n\x00\n'.join(n + '\x01' + '\n'.join(b) for n, b in funcs))
    out = []
    for part in blob.split('\n\x00\n'):
        name, _, body = part.partition('\x01')
        out.append({'name': name, 'asm': body})
    user = [f for f in out if re.match(r'^<?&?(mut )?main::', f['name'])]
    user.sort(key=lambda f: f['name'] == 'main::main')
    return user or out[:40]


# ---------------------------------------------------------------- DWARF type map (for the tracer)
DIE = re.compile(r'^\s*<(\d+)><([0-9a-f]+)>: Abbrev Number: (\d+)(?: \((DW_TAG_\w+)\))?')
ATTR = re.compile(r'^\s*<[0-9a-f]+>\s+(DW_AT_\w+)\s*: (.*)$')
def strval(v):
    v = re.sub(r'^\((?:strp|string|line_strp|strx\d?|data\d|udata|sdata|ref\d|ref_udata|ref_addr)\)\s*', '', v)
    m = re.match(r'^\((?:indirect (?:line )?string, )?offset: 0x[0-9a-f]+\): (.*)$', v) or re.match(r'^\((?:indirect|indexed) [^)]*\): (.*)$', v)
    return (m.group(1) if m else v).strip()
def build_typemap(binary, src='main.rs'):
    p = subprocess.Popen([TC.readelf, '--debug-dump=info', '--wide', binary], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, errors='replace')
    dies = {}; order = []; cus = 0; in_main = False; cur = None
    for line in p.stdout:
        m = DIE.match(line)
        if m:
            depth, off, abbrev, tag = int(m.group(1)), int(m.group(2), 16), m.group(3), m.group(4)
            if tag == 'DW_TAG_compile_unit':
                if in_main: break
                cus += 1; cur = {'tag': tag, 'depth': depth}; dies[off] = cur; order.append(off); continue
            if abbrev == '0' or not tag:
                cur = None; continue
            cur = {'tag': tag, 'depth': depth}; dies[off] = cur; order.append(off); continue
        if cur is None: continue
        a = ATTR.match(line)
        if not a: continue
        k, v = a.group(1), a.group(2)
        if k == 'DW_AT_name':
            cur['name'] = strval(v)
            if cur['tag'] == 'DW_TAG_compile_unit':
                in_main = cur['name'].split('/@/')[0].endswith(src) and '/rustc/' not in cur['name']
        elif k == 'DW_AT_type':
            mm = re.search(r'<0x([0-9a-f]+)>', v)
            if mm: cur['type'] = int(mm.group(1), 16)
        elif k in ('DW_AT_specification', 'DW_AT_abstract_origin'):
            mm = re.search(r'<0x([0-9a-f]+)>', v)
            if mm: cur['spec'] = int(mm.group(1), 16)
        elif k == 'DW_AT_decl_line':
            try: cur['line'] = int(strval(v).split()[0], 0)
            except ValueError: pass
    p.kill()
    # walk in order with a depth stack for namespaces / subprograms / structs
    out_vars, members = {}, {}
    stack = []
    def tname(off, n=0):
        d = dies.get(off)
        if not d or n > 8: return None
        if 'name' in d: return d['name']
        if d['tag'] in ('DW_TAG_typedef', 'DW_TAG_const_type', 'DW_TAG_volatile_type'): return tname(d.get('type'), n + 1)
        return None
    for off in order:
        d = dies[off]
        while stack and stack[-1]['depth'] >= d['depth']: stack.pop()
        if d['tag'] in ('DW_TAG_variable', 'DW_TAG_formal_parameter') and 'name' not in d and d.get('spec') in dies:
            o = dies[d['spec']]
            for kk in ('name', 'type', 'line'):
                if kk in o and kk not in d: d[kk] = o[kk]
        if d['tag'] in ('DW_TAG_variable', 'DW_TAG_formal_parameter') and 'name' in d and 'type' in d:
            fn = next((s for s in reversed(stack) if s['tag'] == 'DW_TAG_subprogram'), None)
            if fn and 'name' not in fn and fn.get('spec') in dies and 'name' in dies[fn['spec']]:
                fn['name'] = dies[fn['spec']]['name']
            if fn and 'name' in fn:
                t = tname(d['type'])
                if t: out_vars['%s|%s|%d' % (fn['name'], d['name'], d.get('line', 0))] = t
        elif d['tag'] == 'DW_TAG_member' and 'name' in d and 'type' in d:
            owner = stack[-1] if stack else None
            if owner and owner['tag'] in ('DW_TAG_structure_type', 'DW_TAG_union_type') and 'name' in owner:
                ns = '::'.join(s['name'] for s in stack if s['tag'] in ('DW_TAG_namespace', 'DW_TAG_structure_type') and 'name' in s)
                t = tname(d['type'])
                if t and (t.startswith(('&', '*', 'alloc::boxed::Box<'))):
                    members['%s|%s' % (ns, d['name'])] = t
        stack.append(d)
    return {'vars': out_vars, 'members': members}


# ---------------------------------------------------------------- actions
def rust_single(code, action, stdin, edition, timeout):
    if not TC.rustc:
        return {'ok': False, 'phase': 'setup', 'stderr': 'rustc was not found. Install Rust from https://rustup.rs and restart this server.'}
    crates = detect_crates(code)
    if crates and action in ('run', 'release', 'test', 'check', 'clippy'):
        return rust_cargo_scratch(code, action, stdin, edition, timeout, crates)
    with tempfile.TemporaryDirectory(prefix='rustforge-') as td:
        src = Path(td) / 'main.rs'
        src.write_text(code, encoding='utf-8')
        out = Path(td) / ('main' + EXE)
        base = ['--edition', edition, '--error-format=json', '--crate-name', 'main']
        res = {'action': action, 'crates': []}
        if action == 'fmt':
            if not TC.rustfmt:
                return {'ok': False, 'phase': 'setup', 'stderr': 'rustfmt not found: run `rustup component add rustfmt`'}
            r = run_proc([TC.rustfmt, '--edition', edition, str(src)], cwd=td, timeout=30)
            return {'ok': r['code'] == 0, 'phase': 'fmt', 'formatted': src.read_text(encoding='utf-8'), 'stderr': r['stderr']}
        if action == 'asm':
            args = [TC.rustc, *base, '-O', '-C', 'debuginfo=0', '--emit', 'asm', '-o', 'main.s', 'main.rs']
            if TC.info.get('arch', '').lower() in ('x86_64', 'amd64'):
                args[1:1] = ['-C', 'llvm-args=-x86-asm-syntax=intel']
            r = run_proc(args, cwd=td, timeout=120)
            rendered, diags, other = parse_diags(r['stderr'])
            ok = r['code'] == 0
            res.update(ok=ok, phase='compile', compile_ms=r['ms'], rendered=rendered, diags=diags, stderr=other)
            if ok:
                res['functions'] = clean_asm((Path(td) / 'main.s').read_text(encoding='utf-8', errors='replace'))
            return res
        compiler = TC.clippy if action == 'clippy' else TC.rustc
        if action == 'clippy' and not compiler:
            return {'ok': False, 'phase': 'setup', 'stderr': 'clippy not found: run `rustup component add clippy`'}
        args = [compiler, *base]
        if action == 'release':
            args += ['-C', 'opt-level=3']
        if action == 'test':
            args += ['--test']
        if action in ('check', 'clippy'):
            args += ['--emit=metadata', '-o', 'libmain.rmeta', 'main.rs']
        else:
            args += ['-o', out.name, 'main.rs']
        r = run_proc(args, cwd=td, timeout=180)
        rendered, diags, other = parse_diags(r['stderr'])
        ok = r['code'] == 0
        res.update(ok=ok, phase='compile', compile_ms=r['ms'], rendered=rendered, diags=diags, stderr=other, timed_out=r['timed_out'])
        if not ok or action in ('check', 'clippy'):
            return res
        return execute(res, [str(out)], td, stdin, timeout)


def execute(res, cmd, cwd, stdin, timeout, env=None):
    env = env or dict(os.environ, RUST_BACKTRACE='0')
    r = run_proc(cmd, cwd=cwd, stdin=stdin, timeout=timeout, env=env)
    res.update(phase='run', ok=(r['code'] == 0 and not r['timed_out']), exit=r['code'], stdout=r['stdout'],
               stderr=(res.get('stderr') or '') + r['stderr'], run_ms=r['ms'], timed_out=r['timed_out'])
    if r['timed_out']:
        res['stderr'] += f'\n⏱ Stopped after {timeout:g}s (infinite loop? waiting for input?)'
    elif r['code'] not in (0, None) and r['code'] < 0 and not IS_WIN:
        res['stderr'] += f'\n(process killed by signal {-r["code"]})'
    return res


def ensure_cargo_project(path: Path, name: str):
    if (path / 'Cargo.toml').exists():
        return None
    path.parent.mkdir(parents=True, exist_ok=True)
    r = run_proc([TC.cargo, 'new', '--vcs', 'none', '--name', name, str(path)], cwd=str(path.parent), timeout=60)
    return r if r['code'] != 0 else None


def rust_cargo_scratch(code, action, stdin, edition, timeout, crates):
    if not TC.cargo:
        return {'ok': False, 'phase': 'setup', 'stderr': 'This snippet uses crates (' + ', '.join(crates) + ') but cargo was not found.'}
    proj = WS / 'scratch'
    with SCRATCH_LOCK:
        err = ensure_cargo_project(proj, 'scratch')
        if err:
            return {'ok': False, 'phase': 'setup', 'stderr': err['stderr']}
        deps = '\n'.join(f'{c} = {KNOWN_CRATES.get(c, chr(34) + "*" + chr(34))}' for c in crates)
        (proj / 'Cargo.toml').write_text(f'[package]\nname = "scratch"\nversion = "0.1.0"\nedition = "{edition}"\n\n[dependencies]\n{deps}\n', encoding='utf-8')
        (proj / 'src' / 'main.rs').write_text(code, encoding='utf-8')
        sub = {'run': 'build', 'release': 'build', 'test': 'test', 'check': 'check', 'clippy': 'clippy'}[action]
        args = [TC.cargo, sub, '--message-format=json']
        if action == 'release':
            args.append('--release')
        if action == 'test':
            args.append('--no-run')
        r = run_proc(args, cwd=str(proj), timeout=900)
        rendered, diags, exe = [], [], None
        for line in r['stdout'].splitlines():
            try:
                m = json.loads(line)
            except ValueError:
                continue
            if m.get('reason') == 'compiler-message' and m.get('target', {}).get('name') == 'scratch':
                add_diag(m['message'], diags, rendered)
            if m.get('reason') == 'compiler-artifact' and m.get('executable') and m.get('target', {}).get('name') == 'scratch':
                exe = m['executable']
        tail = '\n'.join(l for l in r['stderr'].splitlines() if not re.match(r'\s*(Compiling|Checking|Finished|Downloaded|Downloading|Updating|Locking|Adding|Blocking|Running|Executable)\b', l))
        res = {'action': action, 'crates': crates, 'ok': r['code'] == 0, 'phase': 'compile', 'compile_ms': r['ms'], 'rendered': '\n\n'.join(rendered).strip(),
               'diags': diags, 'stderr': tail.strip(), 'via': 'cargo', 'timed_out': r['timed_out']}
        if not res['ok'] or action in ('check', 'clippy') or not exe:
            return res
    return execute(res, [exe], str(proj), stdin, timeout)


def trace(code, edition, max_snaps):
    if not TC.rustc:
        return {'ok': False, 'phase': 'setup', 'stderr': 'rustc was not found.'}
    if not TC.info.get('gdb'):
        return {'ok': False, 'phase': 'setup', 'stderr': 'The memory tracer needs gdb and readelf on Linux: sudo apt install gdb binutils (or your distro\'s equivalent), then restart serve.py.'}
    tracer = HERE / 'rf_tracer.py'
    with tempfile.TemporaryDirectory(prefix='rustforge-trace-') as td:
        Path(td, 'main.rs').write_text(code, encoding='utf-8')
        r = run_proc([TC.rustc, '--edition', edition, '--error-format=json', '--crate-name', 'main', '-g', '-C', 'opt-level=0',
                      '-C', 'debug-assertions=on', '-A', 'warnings', '-o', 'main', 'main.rs'], cwd=td, timeout=180)
        rendered, diags, other = parse_diags(r['stderr'])
        if r['code'] != 0:
            return {'ok': False, 'phase': 'compile', 'rendered': rendered, 'diags': diags, 'stderr': other, 'compile_ms': r['ms']}
        t0 = time.perf_counter()
        try:
            tm = build_typemap(str(Path(td, 'main')))
        except Exception as e:
            tm = {'vars': {}, 'members': {}}
        Path(td, 'types.json').write_text(json.dumps(tm))
        env = dict(os.environ, RF_OUT=str(Path(td, 'trace.json')), RF_STDOUT=str(Path(td, 'out.txt')), RF_STDERR=str(Path(td, 'err.txt')),
                   RF_TYPES=str(Path(td, 'types.json')), RF_SRC='main.rs', RF_MAX=str(max_snaps), RF_SECONDS='30', RF_SYSROOT=TC.sysroot or '')
        g = run_proc([TC.gdb, '-batch', '-nx', '-q', '-x', str(tracer), './main'], cwd=td, timeout=75, env=env)
        try:
            res = json.loads(Path(td, 'trace.json').read_text())
        except Exception:
            return {'ok': False, 'phase': 'trace', 'stderr': 'The tracer did not finish.' + ('\n(timed out)' if g['timed_out'] else '') + '\n' + (g['stderr'] or g['stdout'])[-1500:]}
        res.update(ok=not res.get('error'), phase='trace', compile_ms=r['ms'], trace_ms=round((time.perf_counter() - t0) * 1000))
        if res.get('error'):
            res['stderr'] = (res.get('stderr') or '') + '\n' + res['error']
        return res


def haskell(code, stdin, timeout):
    if not TC.runghc:
        return {'ok': False, 'phase': 'setup', 'stderr': 'GHC was not found. Install it with GHCup (https://www.haskell.org/ghcup/) and restart this server.'}
    with tempfile.TemporaryDirectory(prefix='rustforge-hs-') as td:
        (Path(td) / 'Main.hs').write_text(code, encoding='utf-8')
        r = run_proc([TC.runghc, 'Main.hs'], cwd=td, stdin=stdin, timeout=max(timeout, 30))
        compile_err = r['code'] != 0 and re.search(r'Main\.hs:\d+:\d+: error', r['stderr'])
        diags = [{'level': 'error', 'line': int(m.group(1)), 'col': int(m.group(2)), 'message': ''} for m in re.finditer(r'Main\.hs:(\d+):(\d+)(?:-\d+)?: error', r['stderr'])]
        return {'ok': r['code'] == 0 and not r['timed_out'], 'phase': 'compile' if compile_err else 'run', 'stdout': r['stdout'], 'stderr': '' if compile_err else r['stderr'],
                'rendered': r['stderr'].strip() if compile_err else '', 'diags': diags, 'exit': r['code'], 'run_ms': r['ms'], 'timed_out': r['timed_out']}


SAFE_SUB = {'build', 'run', 'test', 'check', 'clippy', 'fmt', 'bench', 'doc', 'tree', 'update', 'add', 'remove', 'clean'}


def project(req):
    pid = req.get('id', '')
    if not re.fullmatch(r'[a-z0-9][a-z0-9\-]{0,60}', pid):
        return {'ok': False, 'stderr': 'bad project id'}
    path = WS / 'projects' / pid
    act = req.get('action')
    if act == 'status':
        files = sorted(str(p.relative_to(path)).replace('\\', '/') for p in path.rglob('*') if p.is_file() and 'target' not in p.parts)[:200] if path.exists() else []
        return {'ok': True, 'exists': (path / 'Cargo.toml').exists(), 'path': str(path), 'files': files}
    if not TC.cargo:
        return {'ok': False, 'stderr': 'cargo was not found. Install Rust from https://rustup.rs'}
    lock = PROJ_LOCKS.setdefault(pid, threading.Lock())
    with lock:
        if act == 'init':
            crate = pid.replace('-', '_')
            err = ensure_cargo_project(path, crate)
            if err:
                return {'ok': False, 'stderr': err['stderr']}
            log = [f'Created {path}']
            for dep in req.get('deps', [])[:12]:
                name, feats = dep.get('name', ''), [f for f in dep.get('features', []) if re.fullmatch(r'[\w\-]+', f)]
                if not re.fullmatch(r'[A-Za-z0-9_\-]+(@[\w.\-^~=*]+)?', name):
                    continue
                args = [TC.cargo, 'add', name] + (['--features', ','.join(feats)] if feats else [])
                r = run_proc(args, cwd=str(path), timeout=180)
                log.append(('✓ ' if r['code'] == 0 else '✗ ') + 'cargo add ' + name + ('' if r['code'] == 0 else '\n' + r['stderr'].strip()))
            return {'ok': True, 'path': str(path), 'log': '\n'.join(log)}
        if act == 'open':
            if not path.exists():
                return {'ok': False, 'stderr': 'Create the project first.'}
            code_cmd = which('code', 'code-insiders', 'codium')
            try:
                if code_cmd and req.get('editor', True):
                    subprocess.Popen([code_cmd, str(path)], shell=IS_WIN)
                elif IS_WIN:
                    os.startfile(str(path))  # type: ignore[attr-defined]
                elif sys.platform == 'darwin':
                    subprocess.Popen(['open', str(path)])
                else:
                    subprocess.Popen(['xdg-open', str(path)])
            except Exception as e:
                return {'ok': False, 'stderr': str(e)}
            return {'ok': True, 'path': str(path), 'opened': 'VS Code' if code_cmd else 'file manager'}
        if act == 'cargo':
            if not (path / 'Cargo.toml').exists():
                return {'ok': False, 'stderr': 'Create the project first.'}
            try:
                argv = shlex.split(req.get('cmd', ''), posix=True)
            except ValueError as e:
                return {'ok': False, 'stderr': str(e)}
            if argv[:1] == ['cargo']:
                argv = argv[1:]
            if not argv or argv[0] not in SAFE_SUB:
                return {'ok': False, 'stderr': 'Only these cargo subcommands can be run from the page: ' + ', '.join(sorted(SAFE_SUB))}
            env = dict(os.environ, CARGO_TERM_COLOR='never')
            t = float(req.get('timeout', 900 if argv[0] != 'run' else 60))
            r = run_proc([TC.cargo, *argv], cwd=str(path), stdin=req.get('stdin', ''), timeout=min(t, 1800), env=env)
            summary = re.findall(r'test result: (\w+)\. (\d+) passed; (\d+) failed', r['stdout'])
            return {'ok': r['code'] == 0 and not r['timed_out'], 'exit': r['code'], 'stdout': r['stdout'], 'stderr': r['stderr'], 'ms': r['ms'],
                    'timed_out': r['timed_out'], 'tests': [{'result': a, 'passed': int(b), 'failed': int(c)} for a, b, c in summary]}
    return {'ok': False, 'stderr': 'unknown action'}


# ---------------------------------------------------------------- HTTP
class H(BaseHTTPRequestHandler):
    server_version = 'ZeroCosts/1.0'

    def log_message(self, fmt, *a):
        if '/api/' in (self.path or ''):
            sys.stderr.write('  %s %s\n' % (self.command, self.path))

    def host_ok(self):
        host = (self.headers.get('Host') or '').lower()
        port = PUBLIC_PORT or self.server.server_address[1]
        return host in (f'127.0.0.1:{port}', f'localhost:{port}', f'[::1]:{port}')

    def send(self, code, body, ctype='application/json; charset=utf-8', extra=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
        elif isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        for k, v in dict(self.cors(), **(extra or {})).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def origin_allowed(self):
        o = self.headers.get('Origin')
        return bool(SITE and o and o.rstrip('/') == SITE)

    def cors(self):
        if self.origin_allowed():
            return {'Access-Control-Allow-Origin': SITE, 'Vary': 'Origin', 'Access-Control-Allow-Headers': 'X-Forge-Token, Content-Type',
                    'Access-Control-Allow-Methods': 'GET, POST, OPTIONS', 'Access-Control-Allow-Private-Network': 'true', 'Access-Control-Max-Age': '600'}
        return {}

    def api_ok(self):
        if not self.host_ok():
            self.send(403, {'error': 'bad host'})
            return False
        origin = self.headers.get('Origin')
        if origin and not self.origin_allowed() and urlparse(origin).netloc.lower() != (self.headers.get('Host') or '').lower():
            self.send(403, {'error': 'cross-origin request refused'})
            return False
        if not secrets.compare_digest(self.headers.get('X-Forge-Token', ''), TOKEN):
            self.send(403, {'error': 'missing or wrong pairing code'}, extra=self.cors())
            return False
        return True

    def do_OPTIONS(self):  # CORS preflight: only the configured site origin is ever allowed
        if self.host_ok() and self.origin_allowed():
            self.send(204, b'')
        else:
            self.send(403, {'error': 'no'})

    def do_GET(self):
        if not self.host_ok():
            return self.send(403, 'bad host', 'text/plain')
        path = unquote(urlparse(self.path).path)
        if path == '/api/hello':  # no secrets here: lets the site discover the helper before pairing
            if self.origin_allowed() or not self.headers.get('Origin'):
                return self.send(200, {'forge': True, 'helper': 1, 'paired_needed': True})
            return self.send(403, {'error': 'origin not allowed'})
        if path == '/api/env':
            if not self.api_ok():
                return
            return self.send(200, dict(TC.info, workspace=str(WS)))
        if SITE and not (WEB / 'index.html').exists():
            return self.send(404, 'This is the ZeroCosts helper. Open the course in your browser.', 'text/plain')
        if path == '/':
            path = '/index.html'
        f = (WEB / path.lstrip('/')).resolve()
        if WEB not in f.parents and f != WEB or not f.is_file():
            return self.send(404, 'not found', 'text/plain')
        data = f.read_bytes()
        if f.name == 'index.html':
            data = data.replace(b'<head>', b'<head><meta name="forge-local" content="' + TOKEN.encode() + b'">', 1)
        self.send(200, data, MIME.get(f.suffix, 'application/octet-stream'),
                  {'Content-Security-Policy': "frame-ancestors 'none'", 'Referrer-Policy': 'no-referrer'})

    def do_POST(self):
        path = urlparse(self.path).path
        if not self.api_ok():
            return
        try:
            n = int(self.headers.get('Content-Length', 0))
            if n > 2_000_000:
                return self.send(413, {'error': 'too large'})
            req = json.loads(self.rfile.read(n) or b'{}')
        except Exception:
            return self.send(400, {'error': 'bad json'})
        try:
            if path == '/api/run':
                code, lang = str(req.get('code', '')), req.get('lang', 'rust')
                stdin, timeout = str(req.get('stdin', '')), max(1.0, min(float(req.get('timeout', 10)), 120.0))
                if lang == 'haskell':
                    return self.send(200, haskell(code, stdin, timeout))
                action = req.get('action', 'run')
                if action not in ('run', 'release', 'test', 'check', 'clippy', 'fmt', 'asm'):
                    return self.send(400, {'error': 'bad action'})
                edition = req.get('edition') if req.get('edition') in ('2015', '2018', '2021', '2024') else TC.edition
                if edition == '2024' and TC.minor < 85:
                    edition = '2021'
                return self.send(200, rust_single(code, action, stdin, edition, timeout))
            if path == '/api/trace':
                edition = req.get('edition') if req.get('edition') in ('2021', '2024') else TC.edition
                return self.send(200, trace(str(req.get('code', '')), edition, max(10, min(int(req.get('max', 250)), 600))))
            if path == '/api/project':
                return self.send(200, project(req))
            if path == '/api/refresh':
                TC.__init__()
                return self.send(200, TC.info)
            return self.send(404, {'error': 'unknown endpoint'})
        except Exception as e:  # report, don't crash the server
            return self.send(500, {'ok': False, 'phase': 'server', 'stderr': f'server error: {e!r}'})


def main():
    global TC, WS, SITE, TOKEN, PUBLIC_PORT
    ap = argparse.ArgumentParser(description='ZeroCosts local server / helper')
    ap.add_argument('--port', type=int, default=8765)
    ap.add_argument('--open', action='store_true', help='open the browser')
    ap.add_argument('--host', default='127.0.0.1', help='address to listen on. Only 127.0.0.1 (default) is allowed, except inside the official Docker image where the port is published to the host\'s loopback only')
    ap.add_argument('--public-port', type=int, default=0, help='Docker: the host port the browser connects to (must match -p 127.0.0.1:PORT:PORT)')
    ap.add_argument('--site', default=os.environ.get('ZEROCOSTS_SITE', ''), help='helper mode: https origin of the hosted course allowed to use this machine, e.g. https://zerocosts.example')
    ap.add_argument('--workspace', default=str(Path.home() / 'ZeroCosts'), help='where projects are created (default ~/ZeroCosts)')
    a = ap.parse_args()
    if a.host not in ('127.0.0.1', 'localhost', '::1') and not IN_CONTAINER:
        sys.exit('Refusing to listen on %s: this server runs code, so it only listens on 127.0.0.1 (the Docker image publishes its port to loopback only).' % a.host)
    PUBLIC_PORT = a.public_port
    WS = Path(a.workspace).expanduser().resolve()
    WS.mkdir(parents=True, exist_ok=True)
    SITE = a.site.rstrip('/')
    if SITE:
        if not re.fullmatch(r'https://[A-Za-z0-9.-]+(:\d+)?|http://(localhost|127\.0\.0\.1)(:\d+)?', SITE):
            sys.exit('--site must be an origin like https://zerocosts.example')
        TOKENFILE.parent.mkdir(parents=True, exist_ok=True)
        if TOKENFILE.exists() and TOKENFILE.read_text().strip():
            TOKEN = TOKENFILE.read_text().strip()
        else:
            old = os.umask(0o077)
            try:
                TOKENFILE.write_text(TOKEN + '\n')
                os.chmod(TOKENFILE, 0o600)
            finally:
                os.umask(old)
    if not SITE and not (WEB / 'index.html').exists():
        sys.exit(f'Could not find {WEB / "index.html"} — keep serve.py next to the web/ folder.')
    print('ZeroCosts — checking your toolchain…')
    TC = Toolchain()
    for k in ('rustc', 'cargo', 'rustfmt', 'clippy', 'gdb', 'ghc'):
        print(f'  {k:8} {TC.info.get(k) or "not found"}')
    if not TC.info.get('rustc'):
        print('\n  ⚠ rustc not found. Install Rust from https://rustup.rs, open a NEW terminal, and run this again.\n    The course still works; code just runs in the online Playground instead.')
    srv = None
    for port in ([a.port] if IN_CONTAINER else range(a.port, a.port + 20)):   # in Docker the mapped port is fixed
        try:
            srv = ThreadingHTTPServer((a.host, port), H)
            break
        except OSError:
            continue
    if not srv:
        sys.exit('No free port found.')
    url = f'http://127.0.0.1:{srv.server_address[1]}/'
    if SITE:
        print(f'\n  ▶ Helper ready on port {srv.server_address[1]} for {SITE}')
        print(f'  Pairing code (paste it into the course page once):\n\n      {TOKEN}\n')
        print(f'  Projects go in {WS}\n  Press Ctrl+C to stop.\n')
        if a.open:
            threading.Timer(0.6, lambda: webbrowser.open(SITE + '/#pair=' + TOKEN)).start()
    else:
        print(f'\n  ▶ Open {url}\n  Projects go in {WS}\n  Press Ctrl+C to stop.\n')
        if a.open:
            threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print('\nbye 🦀')


if __name__ == '__main__':
    main()
