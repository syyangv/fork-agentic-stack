"use strict";
const { PassThrough } = require("stream");
const [bridge, label, mode = "full"] = process.argv.slice(2);
if (!bridge || !label) throw new Error("bridge and label required");
const input = new PassThrough();
Object.defineProperty(process, "stdin", { value: input, configurable: true });
process.argv = [process.execPath, bridge, "--agent=hermes", "--no-viewer", "--runtime-scope=0123456789abcdef"];
const realWrite = process.stdout.write.bind(process.stdout);
let buffer = "";
let episodeId = null;
const project = "0123456789abcdef";
const sessionId = `arm64-${label}`;
const namespace = {agentKind:"hermes",profileId:project,workspaceId:project,workspacePath:`/synthetic/${project}`,sessionKey:sessionId};
const common = {agent:"hermes",sessionId,namespace};
const now = Date.now();
const token = "synthetic-python-case-00-quartz";
let nextId = 1;
function send(method, params) { const id=nextId++; input.write(JSON.stringify({jsonrpc:"2.0",id,method,params})+"\n"); }
function advance(value) {
  if (value.error) { realWrite(JSON.stringify({phase9aDriverError:value.error})+"\n"); process.exit(70); }
  switch (value.id) {
    case 1: send("session.open", {...common,meta:{qualification:true}}); break;
    case 2:
      if (mode === "seed") send("session.close", {sessionId});
      else send("turn.start", {...common,turnKey:`turn-${label}`,userText:`training example python ${token}`,contextHints:{synthetic:true},ts:now});
      break;
    case 3:
      if (mode === "seed") send("core.shutdown", {});
      else { episodeId=value.result?.query?.episodeId; if(!episodeId) throw new Error("missing episode"); send("turn.end", {...common,episodeId,agentText:"synthetic-solution-python-00-verified",toolCalls:[],contextHints:{synthetic:true},ts:now+1}); }
      break;
    case 4:
      if (mode === "seed") { realWrite(JSON.stringify({phase9aDriverComplete:true,label,episodeId:null,searchHits:"not-applicable"})+"\n"); setTimeout(()=>process.exit(0),500); }
      else send("episode.close", {episodeId});
      break;
    case 5: send("session.close", {sessionId}); break;
    case 6: send("memory.search", {agent:"hermes",namespace,query:`recall training solution for ${token}`,topK:{tier1:5,tier2:5,tier3:5},filters:{reason:"arm64_xctrace"}}); break;
    case 7:
      if (!(value.result?.hits?.length > 0)) throw new Error("search returned no hits");
      send("core.shutdown", {}); break;
    case 8: realWrite(JSON.stringify({phase9aDriverComplete:true,label,episodeId,searchHits:"verified"})+"\n"); setTimeout(()=>process.exit(0),500); break;
  }
}
process.stdout.write = function(chunk, encoding, callback) {
  const text = Buffer.isBuffer(chunk) ? chunk.toString(encoding || "utf8") : String(chunk);
  realWrite(chunk, encoding, callback); buffer += text;
  for (;;) { const at=buffer.indexOf("\n"); if(at<0) break; const line=buffer.slice(0,at); buffer=buffer.slice(at+1); try { const value=JSON.parse(line); if(value && Number.isInteger(value.id)) setImmediate(()=>advance(value)); } catch {} }
  return true;
};
require(bridge);
setTimeout(()=>send("core.health", {}), 2000);
setTimeout(()=>{ realWrite(JSON.stringify({phase9aDriverTimeout:true})+"\n"); process.exit(71); }, 60000).unref();
