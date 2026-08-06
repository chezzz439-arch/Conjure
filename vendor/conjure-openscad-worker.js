/* Conjure — OpenSCAD render worker.
 *
 * One render per worker, then the worker is terminated by the page. That is not
 * tidiness: openscad.wasm.js is an emscripten main() build, and calling
 * callMain() a second time on the same instance throws and writes nothing
 * (measured against release 2022.03.20). A fresh worker per render also makes a
 * long render cancellable — terminate() stops a 45-second boolean mid-flight,
 * which a server subprocess cannot offer as cheaply.
 *
 * The page compiles the 7.7MB wasm once and posts the WebAssembly.Module here,
 * so this only pays instantiation (~15ms), not compilation, per render.
 *
 * Message in : {source, wasmModule?}
 * Message out: {ok:true, stl:ArrayBuffer, ms, log} | {ok:false, error, log}
 */

const GLUE = '/vendor/openscad.wasm.js';
const WASM = '/vendor/openscad.wasm';

function boot(compiled) {
  return new Promise((resolve, reject) => {
    const lines = [];
    const mod = {
      noInitialRun: true,
      print:    (t) => lines.push(t),
      printErr: (t) => lines.push(t),
      onRuntimeInitialized: () => { mod._log = () => lines; resolve(mod); },
      onAbort: (why) => reject(new Error('wasm aborted: ' + why)),
    };
    if (compiled) {
      // Hand emscripten the already-compiled module instead of letting it
      // fetch and compile its own copy on every render.
      mod.instantiateWasm = (imports, done) => {
        WebAssembly.instantiate(compiled, imports)
          .then((instance) => done(instance, compiled))
          .catch(reject);
        return {};
      };
    }
    self.OpenSCAD = mod;
    try {
      importScripts(GLUE);
    } catch (e) {
      reject(new Error('could not load ' + GLUE + ': ' + e.message));
    }
  });
}

self.onmessage = async (ev) => {
  const { source, wasmModule } = ev.data || {};
  const t0 = performance.now();
  let log = [];
  try {
    if (typeof source !== 'string' || !source.trim()) {
      throw new Error('no source to render');
    }
    let compiled = wasmModule;
    if (!compiled) {
      // The page could not pass a compiled module (older engine, or it never
      // got one). Compiling here still works, it just costs more.
      const resp = await fetch(WASM);
      if (!resp.ok) throw new Error(WASM + ' unavailable (HTTP ' + resp.status + ')');
      compiled = await WebAssembly.compile(await resp.arrayBuffer());
    }

    const inst = await boot(compiled);
    inst.FS.writeFile('/in.scad', source);

    let exit = 0;
    try {
      // No --enable=manifold. The 2022.03.20 wasm answers it with "Ignoring
      // request to enable unknown feature", and the 2021.01 CLI on the server
      // side rejects it outright — so neither renderer gains anything and one
      // of them would fail.
      exit = inst.callMain(['/in.scad', '-o', '/out.stl']);
    } catch (e) {
      exit = (e && e.status !== undefined) ? e.status : 1;
    }
    log = inst._log ? inst._log() : [];

    let stl = null;
    try { stl = inst.FS.readFile('/out.stl'); } catch (_) {}
    if (exit !== 0 || !stl || stl.length < 100) {
      // A parse error, an empty top-level object, or text() without the fonts
      // pack all land here. The page falls back to the server renderer, which
      // has fonts and a different OpenSCAD build.
      const why = log.filter(l => /^(ERROR|WARNING)|Fontconfig|empty/i.test(l)).slice(0, 4);
      throw new Error(why.join(' | ') || ('openscad exited ' + exit));
    }

    const buf = stl.buffer.slice(stl.byteOffset, stl.byteOffset + stl.byteLength);
    self.postMessage({ ok: true, stl: buf, ms: performance.now() - t0, log }, [buf]);
  } catch (e) {
    self.postMessage({ ok: false, error: e.message || String(e),
                       ms: performance.now() - t0, log });
  }
};
