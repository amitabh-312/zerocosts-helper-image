# ZeroCosts — mechanical memory tracer. Runs INSIDE gdb:
#   gdb -batch -nx -x rf_tracer.py ./main
# Environment: RF_OUT (json path), RF_STDOUT, RF_STDERR, RF_TYPES (json typemap), RF_MAX (snapshots), RF_SRC (source file name)
# It single-steps the program through the user's source file, and after every step reads the real
# stack frames, variables, pointers and heap blocks from the running process. No guessing, no AI.
import gdb, json, os, re, sys, time
if os.environ.get('RF_SYSROOT'):
    sys.path.insert(0, os.path.join(os.environ['RF_SYSROOT'], 'lib', 'rustlib', 'etc'))

OUT = os.environ['RF_OUT']
SRC = os.environ.get('RF_SRC', 'main.rs')
MAX_SNAPS = int(os.environ.get('RF_MAX', '250'))
MAX_STEPS = int(os.environ.get('RF_MAX_STEPS', '6000'))
DEADLINE = time.time() + float(os.environ.get('RF_SECONDS', '40'))
try:
    TM = json.load(open(os.environ['RF_TYPES']))
except Exception:
    TM = {'vars': {}, 'members': {}}
VARS, MEMBERS = TM.get('vars', {}), TM.get('members', {})

snaps, events, notes = [], [], []
live = {}        # heap address -> size (from malloc/realloc/free breakpoints)
freed = {}       # heap address -> size, freed since tracing started (until reused)
result = {'snaps': snaps, 'notes': notes}
ARCH = ''
REGS = []


def reg(i):
    return int(gdb.parse_and_eval('$' + REGS[i])) & 0xFFFFFFFFFFFFFFFF


def ret_reg():
    return int(gdb.parse_and_eval('$' + ('rax' if 'x86-64' in ARCH else 'x0'))) & 0xFFFFFFFFFFFFFFFF


class Ret(gdb.FinishBreakpoint):
    def __init__(self, kind, args):
        super().__init__(gdb.newest_frame(), internal=True)
        self.silent = True
        self.kind, self.args = kind, args

    def stop(self):
        try:
            p = ret_reg()
        except Exception:
            return False
        if self.kind == 'realloc':
            old, size = self.args
            if old:
                live.pop(old, None)
                if p != old:
                    freed[old] = 0
                    events.append({'k': 'free', 'a': old, 'why': 'realloc'})
            live[p] = size
            freed.pop(p, None)
            events.append({'k': 'realloc' if old else 'alloc', 'a': p, 's': size, 'from': old})
        else:
            if p:
                live[p] = self.args[0]
                freed.pop(p, None)
                events.append({'k': 'alloc', 'a': p, 's': self.args[0]})
        return False

    def out_of_scope(self):
        pass


class Alloc(gdb.Breakpoint):
    def __init__(self, spec, kind):
        super().__init__(spec, internal=True)
        self.silent = True
        self.kind = kind

    def stop(self):
        try:
            if self.kind == 'free':
                p = reg(0)
                if p:
                    sz = live.pop(p, None)
                    freed[p] = sz or 0
                    events.append({'k': 'free', 'a': p, 's': sz})
            elif self.kind == 'malloc':
                Ret('alloc', (reg(0),))
            elif self.kind == 'calloc':
                Ret('alloc', (reg(0) * reg(1),))
            elif self.kind == 'realloc':
                Ret('realloc', (reg(0), reg(1)))
            elif self.kind == 'memalign':  # posix_memalign(&out, align, size): read *out afterwards
                pass
        except Exception as e:
            notes.append('alloc hook: %s' % e)
        return False


PANIC = [False]
HOOKS = []


class PanicBP(gdb.Breakpoint):
    def __init__(self, spec):
        super().__init__(spec, internal=True)
        self.silent = True

    def stop(self):
        PANIC[0] = True
        for h in HOOKS:      # the panic machinery allocates a lot; stop tracking it
            try:
                h.enabled = False
            except Exception:
                pass
        return True


# ------------------------------------------------------------------ helpers
def is_user(frame):
    try:
        sal = frame.find_sal()
        return bool(sal.symtab) and os.path.basename(sal.symtab.filename) == SRC and '/rustc/' not in sal.symtab.filename and 'library/' not in sal.symtab.filename
    except Exception:
        return False


def user_frames():
    out, f = [], gdb.newest_frame()
    while f is not None:
        if is_user(f):
            out.append(f)
        try:
            f = f.older()
        except Exception:
            break
    return list(reversed(out))


PATH_RE = re.compile(r'\b(?:[a-z_][a-z0-9_]*::)+')


def short(t):
    s = str(t)
    s = re.sub(r',\s*alloc::alloc::Global', '', s)
    s = s.replace('alloc::alloc::Global', '')
    s = PATH_RE.sub('', s)
    s = s.replace('RcInner<', 'RcBox<')
    return s[:60]


def tname(t):
    try:
        return t.name or str(t)
    except Exception:
        return '?'


STACK = [0, 0]


def region(addr):
    if not addr or addr < 4096:
        return 'null'
    if STACK[0] <= addr < STACK[1]:
        return 'stack'
    for a, s in live.items():
        if a <= addr < a + max(s, 1):
            return 'heap'
    if addr in freed:
        return 'freed'
    for lo, hi, name in MAPS:
        if lo <= addr < hi:
            if name == '[heap]':
                return 'heap?'
            if name.startswith('/') or name == '':
                return 'static' if name.startswith('/') else 'anon'
    return 'unknown'


MAPS = []


def read_maps():
    global MAPS
    MAPS = []
    try:
        pid = gdb.selected_inferior().pid
        for line in open('/proc/%d/maps' % pid):
            parts = line.split()
            lo, hi = (int(x, 16) for x in parts[0].split('-'))
            name = parts[5] if len(parts) > 5 else ''
            MAPS.append((lo, hi, name))
            if name == '[stack]':
                STACK[0], STACK[1] = lo, hi
    except Exception as e:
        notes.append('maps: %s' % e)


def ptr_kind(dwarf_name):
    if not dwarf_name:
        return None
    if dwarf_name.startswith('&mut '):
        return 'mref'
    if dwarf_name.startswith('&'):
        return 'ref'
    if dwarf_name.startswith('*'):
        return 'raw'
    if dwarf_name.startswith('alloc::boxed::Box<'):
        return 'own'
    if dwarf_name.startswith(('core::ptr::non_null::NonNull<', 'core::ptr::unique::Unique<')):
        return 'raw'
    return None


def fmt_prim(v, t):
    c = t.code
    try:
        if c == gdb.TYPE_CODE_BOOL:
            return 'true' if int(v) else 'false'
        if c == gdb.TYPE_CODE_CHAR:
            n = int(v)
            return "'%s'" % (chr(n) if 32 <= n < 0x110000 and n != 39 else '\\u{%x}' % n)
        if c == gdb.TYPE_CODE_FLT:
            f = float(v)
            return repr(f) if abs(f) < 1e15 else '%g' % f
        return str(int(v))
    except Exception:
        return str(v)


def read_str(addr, n):
    try:
        b = bytes(gdb.selected_inferior().read_memory(addr, min(n, 200)))
        s = b.decode('utf-8', 'replace')
        return s
    except Exception:
        return None


# ------------------------------------------------------------------ value walking
class Ctx:
    def __init__(self):
        self.pending = []   # (addr, spec) heap/static targets to materialise
        self.blocks = {}    # addr -> block
        self.owners = []    # (slot, rank) owning pointers, for move detection


def kid(slot):
    return slot


def walk(val, name, ctx, depth=0, dname=None, rank=0):
    """Return a slot dict describing `val` (which lives at val.address)."""
    try:
        t = val.type.strip_typedefs()
    except Exception:
        return {'n': name, 'ty': '?', 'v': '?', 'k': 'val'}
    slot = {'n': name, 'ty': short(dname if ptr_kind(dname) else tname(val.type)), 'k': 'val'}
    try:
        if val.address is not None:
            slot['a'] = int(val.address)
        slot['sz'] = t.sizeof
    except Exception:
        pass
    code = t.code
    tn = tname(t)
    try:
        if code in (gdb.TYPE_CODE_INT, gdb.TYPE_CODE_BOOL, gdb.TYPE_CODE_CHAR, gdb.TYPE_CODE_FLT):
            slot['v'] = fmt_prim(val, t)
            slot['k'] = 'copy'
            return slot
        if code == gdb.TYPE_CODE_PTR or code == gdb.TYPE_CODE_REF:
            target = int(val) & 0xFFFFFFFFFFFFFFFF
            k = ptr_kind(dname)
            pointee = t.target()
            slot['to'] = target
            slot['v'] = '0x%x' % target
            slot['k'] = k or 'ptr?'
            if not dname:
                slot['ty'] = '*' + short(tname(pointee))
            ctx.pending.append((target, ('one', pointee, k)))
            if k == 'own':
                ctx.owners.append((slot, rank))
            return slot
        if code == gdb.TYPE_CODE_ENUM:
            slot['v'] = short(str(val)).split('::')[-1]
            slot['k'] = 'copy'
            return slot
        if code == gdb.TYPE_CODE_ARRAY:
            r = t.range()
            n = r[1] - r[0] + 1
            kids = [walk(val[i], '[%d]' % i, ctx, depth + 1, rank=rank) for i in range(min(n, 16))]
            slot['kids'] = kids
            slot['v'] = '[' + ', '.join(k.get('v', '?') for k in kids[:8]) + (', …' if n > 8 else '') + ']'
            slot['k'] = 'copy' if all(k['k'] == 'copy' for k in kids) else 'val'
            return slot
        if code in (gdb.TYPE_CODE_STRUCT, gdb.TYPE_CODE_UNION):
            return walk_struct(val, t, tn, slot, ctx, depth, rank)
        slot['v'] = str(val)[:60]
    except gdb.MemoryError:
        slot['v'] = '⟨unreadable⟩'
    except Exception as e:
        slot['v'] = '⟨%s⟩' % str(e)[:40]
    return slot


def field_names(t):
    try:
        return [f for f in t.fields() if f.name and not f.artificial and not f.is_base_class]
    except Exception:
        return []


def unwrap_ptr(v):
    """NonNull / Unique / RawVecInner pointer → integer address."""
    for _ in range(6):
        t = v.type.strip_typedefs()
        if t.code in (gdb.TYPE_CODE_PTR, gdb.TYPE_CODE_INT):
            return int(v) & 0xFFFFFFFFFFFFFFFF
        fs = field_names(t)
        if not fs:
            return 0
        v = v[fs[0]]
    return 0


def vec_parts(v):
    """(data_addr, len, cap, elem_type) for alloc::vec::Vec<T>."""
    length = int(v['len'])
    inner = v['buf']['inner']
    data = unwrap_ptr(inner['ptr'])
    cap = inner['cap']
    try:
        cap = int(cap)
    except Exception:
        cap = int(cap[field_names(cap.type.strip_typedefs())[0].name])
    et = v.type.strip_typedefs().template_argument(0)
    return data, length, cap, et


def walk_struct(val, t, tn, slot, ctx, depth, rank):
    if tn == '&str' or tn.startswith(('&[', '&mut [', '&mut str')) or (field_names(t) and [f.name for f in field_names(t)] == ['data_ptr', 'length']):
        data = int(val['data_ptr']) & 0xFFFFFFFFFFFFFFFF
        n = int(val['length'])
        slot['k'] = 'mref' if tn.startswith('&mut') else 'ref'
        slot['to'] = data
        slot['len'] = n
        if 'str' in tn:
            s = read_str(data, n)
            slot['v'] = '"%s"' % (s if s is not None and len(s) <= 40 else (s or '')[:38] + '…')
            ctx.pending.append((data, ('bytes', n)))
        else:
            et = val['data_ptr'].type.strip_typedefs().target()
            slot['v'] = 'len %d' % n
            ctx.pending.append((data, ('slice', et, n)))
        return slot
    if tn.startswith('alloc::string::String'):
        data, n, cap, _ = vec_parts(val['vec'])
        s = read_str(data, n) if n else ''
        slot.update(k='own', to=data if cap else None, v='"%s"' % ((s or '')[:36] + ('…' if s and len(s) > 36 else '')), len=n, cap=cap)
        if cap:
            ctx.pending.append((data, ('str', n, cap)))
            ctx.owners.append((slot, rank))
        return slot
    if tn.startswith(('alloc::vec::Vec<', 'alloc::collections::vec_deque::VecDeque<')):
        if tn.startswith('alloc::vec::Vec<'):
            data, n, cap, et = vec_parts(val)
        else:
            n = int(val['len'])
            data = unwrap_ptr(val['buf']['inner']['ptr'])
            cap = int(val['buf']['inner']['cap'][field_names(val['buf']['inner']['cap'].type.strip_typedefs())[0].name]) if field_names(val['buf']['inner']['cap'].type.strip_typedefs()) else int(val['buf']['inner']['cap'])
            et = t.template_argument(0)
        slot.update(k='own', v='len %d · cap %d' % (n, cap), len=n, cap=cap)
        if cap and et.sizeof:
            slot['to'] = data
            ctx.pending.append((data, ('vec', et, n, cap)))
            ctx.owners.append((slot, rank))
        return slot
    if tn.startswith(('alloc::rc::Rc<', 'alloc::sync::Arc<', 'alloc::rc::Weak<', 'alloc::sync::Weak<')):
        p = unwrap_ptr(val['ptr'])
        slot.update(k='rc', to=p)
        try:
            inner = val['ptr']
            inner_t = None
            fs = field_names(inner.type.strip_typedefs())
            if fs:
                ptr = inner[fs[0].name]
                inner_t = ptr.type.strip_typedefs().target()
            if inner_t is not None and p:
                iv = gdb.Value(p).cast(inner_t.pointer()).dereference()
                st = int(unwrap_scalar(iv['strong']))
                wk = int(unwrap_scalar(iv['weak'])) - 1
                slot['v'] = 'strong %d · weak %d' % (st, wk)
                ctx.pending.append((p, ('one', inner_t, 'rc')))
        except Exception as e:
            slot['v'] = 'Rc'
        if 'Weak' in tn:
            slot['k'] = 'weak'
        return slot
    if tn.startswith('alloc::boxed::Box<'):  # fat Box<dyn Trait> / Box<[T]>
        fs = [f.name for f in field_names(t)]
        p = unwrap_ptr(val[fs[0]]) if fs else 0
        slot.update(k='own', to=p, v='0x%x' % p)
        if 'dyn ' in tn:
            slot['v'] += ' + vtable'
        ctx.pending.append((p, ('raw', 'dyn object')))
        ctx.owners.append((slot, rank))
        return slot
    if tn.startswith(('std::collections::hash::map::HashMap<', 'std::collections::hash::set::HashSet<', 'alloc::collections::btree::map::BTreeMap<')):
        txt = ''
        try:
            import gdb_lookup  # rust pretty printers, when available
            pp = gdb_lookup.lookup(val)
            if pp:
                items = []
                for i, (k, v) in enumerate(pp.children()):
                    if i >= 24:
                        items.append('…')
                        break
                    items.append(str(v))
                if 'Map' in tn:
                    pairs = [items[i] + ' → ' + items[i + 1] for i in range(0, len(items) - 1, 2)]
                    txt = '{' + ', '.join(pairs) + '}'
                else:
                    txt = '{' + ', '.join(items) + '}'
        except Exception as e:
            txt = ''
        slot.update(k='own', v=(PATH_RE.sub('', txt) or 'map')[:90])
        return slot
    if tn.startswith(('core::cell::Cell<', 'core::cell::UnsafeCell<')):
        inner = val['value']
        if tn.startswith('core::cell::Cell<'):
            inner = inner['value']
        k = walk(inner, 'value', ctx, depth + 1, rank=rank)
        slot.update(k=k['k'], v=k.get('v', ''), kids=k.get('kids'), to=k.get('to'))
        return slot
    if tn.startswith('core::cell::RefCell<'):
        try:
            b = int(unwrap_scalar(val['borrow']['value']['value']))
        except Exception:
            b = 0
        k = walk(val['value']['value'], 'value', ctx, depth + 1, rank=rank)
        state = 'not borrowed' if b == 0 else ('mutably borrowed' if b < 0 else '%d shared borrow%s' % (b, '' if b == 1 else 's'))
        slot.update(k=k['k'], v=k.get('v', ''), kids=[{'n': 'borrow flag', 'ty': 'isize', 'k': 'copy', 'v': '%d (%s)' % (b, state)}, k])
        return slot
    # enums (Option, Result, user enums): pick the active variant from gdb's rendering
    fs = list(t.fields()) if hasattr(t, 'fields') else []
    try:
        rendered = str(val)
    except Exception:
        rendered = ''
    is_enum = any(f.artificial for f in fs) or rendered.startswith(tn + '::')
    if is_enum:
        variant = None
        if rendered.startswith(tn + '::'):
            mm = re.match(r'(\w+)', rendered[len(tn) + 2:])
            variant = mm.group(1) if mm else None
        slot['v'] = variant or short(rendered)
        if variant and any(f.name == variant for f in fs):
            pv = val[variant]
            kids = []
            for f in field_names(pv.type.strip_typedefs()):
                kn = '.' + f.name[2:] if f.name.startswith('__') else f.name
                kids.append(walk(pv[f.name], kn, ctx, depth + 1, MEMBERS.get(tname(pv.type.strip_typedefs()) + '|' + f.name), rank=rank))
            if len(kids) == 1 and kids[0].get('to') and kids[0]['n'].startswith('.'):
                # Some(Box<T>), Ok(&T) …: the variant is just a pointer — draw the arrow from here
                slot.update(to=kids[0]['to'], k=kids[0]['k'], v='%s(→)' % variant, sz=slot.get('sz'))
                for o in range(len(ctx.owners)):
                    if ctx.owners[o][0] is kids[0]:
                        ctx.owners[o] = (slot, ctx.owners[o][1])
                return slot
            if kids:
                slot['kids'] = kids
                inner = ', '.join(k.get('v', '?') for k in kids[:4])
                slot['v'] = '%s(%s)' % (variant, inner) if kids[0]['n'].startswith('.') else '%s { %s }' % (variant, ', '.join('%s: %s' % (k['n'], k.get('v', '?')) for k in kids[:4]))
                if any(k['k'] in ('own', 'rc') or k.get('to') for k in kids):
                    slot['k'] = 'own'
        return slot
    # tuples and plain structs
    kids = []
    for f in field_names(t):
        if f.name in ('phantom', '_marker', 'alloc') and tname(f.type).startswith(('core::marker::PhantomData', 'alloc::alloc::Global')):
            continue
        kn = '.' + f.name[2:] if f.name.startswith('__') else f.name
        kids.append(walk(val[f.name], kn, ctx, depth + 1, MEMBERS.get(tn + '|' + f.name), rank=rank))
    slot['kids'] = kids
    if tn.startswith('(') or (kids and all(k['n'].startswith('.') for k in kids)):
        slot['v'] = '(' + ', '.join(k.get('v', '?') for k in kids[:6]) + ')'
    else:
        slot['v'] = short(tn).split('<')[0] + ' { ' + ', '.join('%s: %s' % (k['n'], k.get('v', '?')) for k in kids[:4]) + (' … }' if len(kids) > 4 else ' }')
    if any(k['k'] in ('own', 'rc') for k in kids):
        slot['k'] = 'own'
    elif kids and all(k['k'] == 'copy' for k in kids):
        slot['k'] = 'val'
    return slot


def unwrap_scalar(v):
    for _ in range(4):
        t = v.type.strip_typedefs()
        if t.code in (gdb.TYPE_CODE_INT, gdb.TYPE_CODE_BOOL):
            return v
        fs = field_names(t)
        if not fs:
            return v
        v = v[fs[0].name]
    return v


# ------------------------------------------------------------------ heap/static blocks
def materialise(ctx):
    blocks = ctx.blocks
    seen = 0
    def prio(item):
        sp = item[1]
        if sp[0] in ('vec', 'str'):
            return 0
        if sp[0] == 'raw' or (sp[0] == 'one' and sp[2] in ('own', 'rc')):
            return 1
        if sp[0] in ('bytes', 'slice'):
            return 2
        return 3
    while ctx.pending and seen < 60:
        ctx.pending.sort(key=prio)
        addr, spec = ctx.pending.pop(0)
        seen += 1
        if not addr or addr < 4096:
            continue
        reg_ = region(addr)
        if reg_ in ('stack', 'null', 'unknown', 'anon'):
            continue
        if any(b['a'] <= addr < b['a'] + max(b['sz'], 1) for b in blocks.values()):
            continue
        if reg_ == 'freed':
            blocks[addr] = {'a': addr, 'sz': freed.get(addr) or 8, 'r': 'freed', 'ty': 'freed memory', 'cells': []}
            continue
        alloc_size = None
        for a, s in live.items():
            if a == addr:
                alloc_size = s
        b = {'a': addr, 'r': 'heap' if reg_.startswith('heap') else 'static', 'cells': []}
        kind = spec[0]
        try:
            if kind == 'str':
                n, cap = spec[1], spec[2]
                b.update(ty='String buffer', sz=cap)
                b['cells'] = str_cells(addr, n, cap)
            elif kind == 'bytes':
                n = spec[1]
                b.update(ty='str bytes' + (' (in the binary)' if b['r'] == 'static' else ''), sz=n)
                b['cells'] = str_cells(addr, n, n)
            elif kind in ('vec', 'slice'):
                et = spec[1]
                n = spec[2]
                cap = spec[3] if kind == 'vec' else n
                es = max(et.sizeof, 1)
                b.update(ty='[%s; %d]' % (short(tname(et)), cap) if kind == 'vec' else '[%s]' % short(tname(et)), sz=es * cap, es=es)
                p = gdb.Value(addr).cast(et.pointer())
                cells = []
                for i in range(min(n, 24)):
                    cells.append(walk((p + i).dereference(), '[%d]' % i, ctx, 1, rank=3))
                for i in range(n, min(cap, n + max(0, 24 - n), n + 12)):
                    cells.append({'n': '[%d]' % i, 'ty': short(tname(et)), 'k': 'spare', 'v': '', 'a': addr + i * es, 'sz': es})
                if cap > len(cells):
                    cells.append({'n': '…', 'k': 'spare', 'v': '+%d more' % (cap - len(cells)), 'ty': ''})
                b['cells'] = cells
            elif kind == 'one':
                pointee, k = spec[1], spec[2]
                if pointee.code == gdb.TYPE_CODE_VOID or pointee.sizeof == 0:
                    continue
                v = gdb.Value(addr).cast(pointee.pointer()).dereference()
                s = walk(v, '*', ctx, 1, rank=3)
                tn = tname(pointee)
                label = short(tn)
                if tn.startswith(('alloc::rc::RcInner<', 'alloc::sync::ArcInner<')):
                    label = ('Rc' if 'rc::' in tn else 'Arc') + ' allocation'
                    cells = []
                    for c in s.get('kids') or []:
                        if c['n'] in ('strong', 'weak'):
                            c['k'] = 'count'
                            try:
                                c['v'] = str(int(c.get('v', '0')) - (1 if c['n'] == 'weak' else 0))
                            except Exception:
                                pass
                        cells.append(c)
                    b['cells'] = cells
                else:
                    b['cells'] = s.get('kids') if s.get('kids') and not s.get('to') and not tn.startswith('(') and s['k'] != 'copy' else [s]
                b.update(ty=label, sz=alloc_size or pointee.sizeof)
            elif kind == 'raw':
                b.update(ty=spec[1], sz=alloc_size or 0, cells=[{'n': '', 'k': 'val', 'v': '(%d bytes)' % (alloc_size or 0)}])
        except gdb.MemoryError:
            b.update(ty='unreadable', sz=0)
        except Exception as e:
            b.update(ty='?', sz=0, err=str(e)[:80])
        if b['r'] == 'static' and kind not in ('bytes', 'slice'):
            continue
        blocks[addr] = b
    return blocks


def str_cells(addr, n, cap):
    try:
        raw = bytes(gdb.selected_inferior().read_memory(addr, min(n, 64)))
    except Exception:
        raw = b''
    cells, i = [], 0
    while i < len(raw):
        c = raw[i]
        ln = 1 if c < 0x80 else 2 if c >> 5 == 6 else 3 if c >> 4 == 14 else 4 if c >> 3 == 30 else 1
        ch = raw[i:i + ln].decode('utf-8', 'replace')
        cells.append({'n': str(i), 'k': 'byte', 'v': ch if ch != ' ' else '␣', 'a': addr + i, 'sz': ln, 'hex': raw[i:i + ln].hex()})
        i += ln
    if n > len(raw):
        cells.append({'n': '…', 'k': 'spare', 'v': '+%d bytes' % (n - len(raw))})
    for j in range(n, min(cap, n + 8)):
        cells.append({'n': str(j), 'k': 'spare', 'v': '', 'a': addr + j, 'sz': 1})
    if cap > n + 8:
        cells.append({'n': '…', 'k': 'spare', 'v': '+%d spare' % (cap - n - 8)})
    return cells


# ------------------------------------------------------------------ snapshot
def frame_vars(f, depth, ctx, cur_line, innermost):
    out = []
    try:
        b = f.block()
    except Exception:
        return out
    fname = f.function().name if f.function() else '?'
    leaf = fname.split('::')[-1]
    seen = set()
    while b is not None:
        for sym in b:
            if not (sym.is_variable or sym.is_argument):
                continue
            nm = sym.name
            if nm.startswith('__') or '$' in nm:
                continue
            key = (nm, sym.line)
            if key in seen:
                continue
            seen.add(key)
            if not sym.is_argument and sym.line and sym.line >= cur_line and not (sym.line == cur_line and not innermost and False):
                continue
            try:
                v = sym.value(f)
            except Exception:
                continue
            dname = VARS.get('%s|%s|%d' % (leaf, nm, sym.line or 0)) or VARS.get('%s|%s|0' % (leaf, nm))
            rank = depth * 100000 + (sym.line or 0)
            s = walk(v, nm, ctx, 0, dname, rank=rank)
            s['line'] = sym.line
            if sym.is_argument:
                s['arg'] = True
            out.append(s)
        if b.function is not None:
            break
        b = b.superblock
    # shadowing: older bindings with the same name are still in memory but unreachable by name
    names = {}
    for s in out:
        names.setdefault(s['n'], []).append(s)
    for lst in names.values():
        if len(lst) > 1:
            lst.sort(key=lambda s: s.get('line') or 0)
            for s in lst[:-1]:
                s['shadowed'] = True
    out.sort(key=lambda s: (not s.get('arg'), s.get('line') or 0))
    return out


def ownership(ctx, blocks):
    """Mechanical move detection: two owners pointing at one allocation → the older one was moved from."""
    by = {}
    for slot, rank in ctx.owners:
        to = slot.get('to')
        if to:
            by.setdefault(to, []).append((rank, slot))
    for to, lst in by.items():
        if len(lst) > 1:
            lst.sort(key=lambda x: x[0])
            for _, s in lst[:-1]:
                s['moved'] = True
                s.pop('kids', None)
    for slot, rank in ctx.owners:
        to = slot.get('to')
        if to and to in freed and to not in live:
            slot['moved'] = True
            slot['dangling'] = True
            slot['v'] = '(freed)'
            slot.pop('kids', None)


last_key = [None]


def snapshot(reason=None):
    if os.environ.get('RF_DEBUG'):
        print('SNAP', flush=True)
    if len(snaps) >= MAX_SNAPS:
        return False
    fr = user_frames()
    if not fr:
        return True
    read_maps()
    ctx = Ctx()
    frames = []
    inner = fr[-1]
    for i, f in enumerate(fr):
        line = f.find_sal().line
        fname = f.function().name if f.function() else '?'
        frames.append({'fn': short(fname), 'line': line, 'vars': frame_vars(f, i, ctx, line, f == inner)})
    blocks = materialise(ctx)
    ownership(ctx, blocks)
    snap = {'line': inner.find_sal().line, 'frames': frames, 'heap': sorted(blocks.values(), key=lambda b: b['a']), 'ev': events[:]}
    try:
        snap['out'] = open(os.environ['RF_STDOUT'], encoding='utf-8', errors='replace').read()[-3000:]
    except Exception:
        snap['out'] = ''
    if reason:
        snap['why'] = reason
    key = json.dumps([snap['line'], frames, snap['heap'], snap['out']], sort_keys=True, default=str)
    if key == last_key[0] and not reason:
        events.clear()
        return True
    last_key[0] = key
    events.clear()
    snaps.append(snap)
    return True


# ------------------------------------------------------------------ driver
def alive():
    try:
        return gdb.selected_inferior().pid > 0 and gdb.selected_thread() is not None
    except Exception:
        return False


def main():
    global ARCH, REGS
    gdb.execute('set pagination off')
    gdb.execute('set confirm off')
    gdb.execute('set print pretty off')
    gdb.execute('set print frame-arguments none')
    gdb.execute('set print entry-values no')
    gdb.execute('set print frame-info short-location')
    try:
        gdb.execute('set print finish off')
    except gdb.error:
        pass
    gdb.execute('set step-mode off')
    gdb.execute('set disable-randomization on')
    try:
        gdb.execute('set debuginfod enabled off')
    except Exception:
        pass
    gdb.execute('set environment RUST_BACKTRACE 0', to_string=True)
    gdb.execute('set environment RUST_LIB_BACKTRACE 0', to_string=True)
    gdb.execute('break main::main', to_string=True)
    gdb.execute('run > %s 2> %s' % (os.environ['RF_STDOUT'], os.environ['RF_STDERR']), to_string=True)
    if not alive():
        result['error'] = 'program did not start'
        return
    ARCH = gdb.selected_inferior().architecture().name()
    REGS = ['rdi', 'rsi', 'rdx', 'rcx'] if 'x86-64' in ARCH else ['x0', 'x1', 'x2', 'x3']
    gdb.execute('delete', to_string=True)
    for spec, kind in () if os.environ.get('RF_NOALLOC') else (('malloc', 'malloc'), ('calloc', 'calloc'), ('realloc', 'realloc'), ('free', 'free')):
        try:
            HOOKS.append(Alloc(spec, kind))
        except Exception as e:
            notes.append('no %s hook: %s' % (spec, e))
    # stop whenever user code is (re-)entered from library code: closures, trait impls, drop impls…
    try:
        if os.environ.get('RF_NOFNBP'):
            raise RuntimeError('disabled')
        listing = gdb.execute('info functions -q -n ^main::', to_string=True)
        for m in re.finditer(r'^(\d+):\s', listing, re.M):
            try:
                gdb.Breakpoint('%s:%s' % (SRC, m.group(1)), internal=True).silent = True
            except Exception:
                pass
    except Exception as e:
        notes.append('fn breakpoints: %s' % e)
    for spec in ('core::panicking::panic_fmt', 'std::panicking::rust_panic_with_hook'):
        try:
            bp = PanicBP(spec)
            if bp.pending:
                bp.delete()
        except Exception:
            continue
    steps = 0
    snapshot()
    while alive() and steps < MAX_STEPS and len(snaps) < MAX_SNAPS:
        if time.time() > DEADLINE:
            result['truncated'] = 'time limit'
            break
        steps += 1
        try:
            if os.environ.get('RF_DEBUG'):
                print('STEP', flush=True)
            gdb.execute('step', to_string=True)
        except gdb.error as e:
            break
        if not alive():
            break
        if PANIC[0]:
            snapshot('panic')
            result['panicked'] = True
            break
        # climb back out of library code into the user's file (a breakpoint on the return address
        # instead of `finish`, which is faster and avoids gdb crashes on some Rust return types)
        guard = 0
        while alive() and not PANIC[0] and not is_user(gdb.newest_frame()) and guard < 20:
            guard += 1
            uf = gdb.newest_frame()
            while uf is not None and not is_user(uf):
                uf = uf.older()
            if uf is None:
                break  # main returned: we are in the runtime
            try:
                top = gdb.newest_frame()
                if top.type() == gdb.INLINE_FRAME or top.pc() == uf.pc():
                    # inlined library code: there is no real call to return from
                    gdb.execute('finish', to_string=True)
                    continue
                bp = gdb.Breakpoint('*0x%x' % uf.pc(), internal=True, temporary=True)
                bp.silent = True
                if os.environ.get('RF_DEBUG'):
                    print('OUT', gdb.newest_frame().name(), flush=True)
                gdb.execute('continue', to_string=True)
                if bp.is_valid():
                    bp.delete()
            except gdb.error:
                break
        if PANIC[0] and not result.get('panicked'):
            snapshot('panic')
            result['panicked'] = True
        if result.get('panicked'):
            break
        if not alive():
            result['lost'] = True   # the program ran to completion while we were stepping out
            break
        if not user_frames():
            result['finished'] = True
            break
        snapshot()
    if steps >= MAX_STEPS or len(snaps) >= MAX_SNAPS:
        result['truncated'] = 'step limit'
    # let the program finish so its output is complete
    try:
        gdb.execute('delete', to_string=True)
        if alive():
            if result.get('truncated') and not result.get('panicked'):
                gdb.execute('kill', to_string=True)
            else:
                gdb.execute('continue', to_string=True)
    except gdb.error:
        pass
    try:
        result['stdout'] = open(os.environ['RF_STDOUT'], encoding='utf-8', errors='replace').read()[-8000:]
        result['stderr'] = open(os.environ['RF_STDERR'], encoding='utf-8', errors='replace').read()[-4000:]
    except Exception:
        pass
    result['steps'] = steps


try:
    main()
except Exception as e:
    import traceback
    result['error'] = '%s\n%s' % (e, traceback.format_exc()[-1500:])
with open(OUT, 'w') as fh:
    json.dump(result, fh, default=str)
