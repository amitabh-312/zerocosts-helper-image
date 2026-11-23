#!/bin/sh
# Quick check that rustc can compile and gdb can trace inside this container. Prints one clear line either way.
T="$(mktemp -d)"; trap 'rm -rf "$T"' EXIT
cat > "$T/t.rs" <<'RS'
fn main() { let v = vec![1, 2, 3]; println!("{}", v.len()); }
RS
if ! rustc -g -o "$T/t" "$T/t.rs" >"$T/c.log" 2>&1; then
  echo "  ✗ self-test: rustc could not compile (see below)"; sed 's/^/    /' "$T/c.log" | head -8; exit 1
fi
if gdb -batch -nx -ex 'break t::main' -ex run -ex next "$T/t" >"$T/g.log" 2>&1 && grep -q 'Breakpoint 1' "$T/g.log"; then
  echo "  ✓ self-test: rustc and the memory tracer's debugger both work"
else
  echo "  ✗ self-test: the debugger could not trace a program (the Memory Tracer will not work live; Run, Cargo and recorded traces still do)."
  echo "    Usually Docker is blocking ptrace. Restart with:  --cap-add=SYS_PTRACE  (the launcher does this), or add  --security-opt seccomp=unconfined"
  sed 's/^/    /' "$T/g.log" | head -6
  exit 1
fi
