#!/usr/bin/env python3
"""Qualify the exact Darwin-arm64 lexical runtime with direct-process xctrace."""
from __future__ import annotations
import argparse,hashlib,importlib.util,json,os,shutil,subprocess,sys,xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any
ROOT=Path(__file__).resolve().parents[1]; sys.path.insert(0,str(ROOT)); PROJECT_ID='0123456789abcdef'; ARM_NODE=Path('/opt/homebrew/opt/node@22/bin/node')
SPEC=importlib.util.spec_from_file_location('canonical_zero',ROOT/'scripts/qualify_memos_zero_egress.py'); canonical=importlib.util.module_from_spec(SPEC); assert SPEC.loader; SPEC.loader.exec_module(canonical)
NETWORK={'connect','connect_nocancel','connectx','sendto','sendto_nocancel','sendmsg','sendmsg_nocancel'}
CHILD_CREATE={'fork','vfork','posix_spawn','posix_spawnp'}
def sha(path:Path)->str:return hashlib.sha256(path.read_bytes()).hexdigest()
def resolve_values(root):return {e.attrib['id']:e.attrib.get('fmt',e.text or '') for e in root.iter() if 'id' in e.attrib}
def parse_syscalls(path:Path,expect_network:bool=False,expect_child:bool=False)->dict[str,Any]:
 root=ET.parse(path).getroot(); refs=resolve_values(root)
 def val(e):return e.attrib.get('fmt') or refs.get(e.attrib.get('ref',''),'')
 attempts=[];child_creations=[];processes=set();total=0
 for row in root.iter('row'):
  call=next((e for e in row if e.tag=='syscall'),None); proc=next((e for e in row if e.tag=='process'),None)
  if call is None or proc is None:continue
  total+=1; name=val(call); processes.add(val(proc)); args=[val(e) for e in row if e.tag=='syscall-arg']
  domain=(args[0].lower() if args else '');internet_socket=domain in {'0x2','0x02','2','0x1e','0x1e (af_inet6)','30'} or 'af_inet' in domain
  if name in NETWORK or (name=='socket' and internet_socket):
   returns=[val(e) for e in row if e.tag=='syscall-return']; attempts.append({'call':name,'arguments':args[:4],'errno':returns[-1:]})
  if name in CHILD_CREATE or name.startswith('posix_spawn'):child_creations.append({'call':name,'arguments':args[:2]})
 if total<=0 or len(processes)!=1:raise RuntimeError(f'incomplete process-bound trace: syscalls={total}, processes={sorted(processes)}')
 if expect_child and not child_creations:raise RuntimeError('child canary did not exercise a child-creation syscall')
 if not expect_child and child_creations:raise RuntimeError(f'untraced child boundary attempted: {child_creations}')
 calls={row['call'] for row in attempts}
 if expect_network:
  if not expect_child and not {'socket','connect','sendto'}.issubset(calls):raise RuntimeError(f'canary coverage incomplete: {sorted(calls)}')
  if not expect_child and any(row['errno'] not in (['0x1'],['1']) for row in attempts if row['call'] in {'connect','sendto'}):raise RuntimeError('canary was not denied with EPERM')
 elif attempts:raise RuntimeError(f'network attempts observed: {attempts}')
 return {'syscall_count':total,'processes':sorted(processes),'network_attempts':attempts,'child_creations':child_creations,'descendant_trace_visibility':False if expect_child else 'not-required-no-child-created'}
def trace_process(label:str,target:list[str],trace_root:Path,profile:Path,expect_network:bool=False,expect_child:bool=False)->dict[str,Any]:
 trace=trace_root/f'{label}.trace';xml=trace_root/f'{label}.syscalls.xml';output=trace_root/f'{label}.target.jsonl';home=trace_root/f'home-{label}';tmp=trace_root/f'tmp-{label}';home.mkdir();tmp.mkdir()
 cmd=['/usr/bin/arch','-arm64','/usr/bin/xcrun','xctrace','record','--instrument','System Call Trace','--time-limit','120s','--output',str(trace),'--target-stdout',str(output),'--launch','--','/usr/bin/sandbox-exec','-f',str(profile),'/usr/bin/env','-i','PATH=/opt/homebrew/opt/node@22/bin:/opt/homebrew/bin:/usr/bin:/bin',f'HOME={home}',f'TMPDIR={tmp}',*target]
 completed=subprocess.run(cmd,text=True,capture_output=True,timeout=150)
 if completed.returncode!=0:raise RuntimeError(f'xctrace failed {label}: {completed.stdout[-500:]} {completed.stderr[-500:]}')
 export=subprocess.run(['/usr/bin/arch','-arm64','/usr/bin/xcrun','xctrace','export','--input',str(trace),'--xpath','/trace-toc/run[@number="1"]/data/table[@schema="syscall"]','--output',str(xml)],text=True,capture_output=True,check=True)
 observation=parse_syscalls(xml,expect_network=expect_network,expect_child=expect_child);lines=output.read_text().splitlines();values=[]
 for line in lines:
  try:values.append(json.loads(line))
  except json.JSONDecodeError:pass
 if expect_network:
  rows=next((v.get('child') if isinstance(v,dict) and v.get('parent') else v for v in values if isinstance(v,list) or (isinstance(v,dict) and v.get('parent'))),[])
  if not rows or not all(row.get('denied') for row in rows):raise RuntimeError(f'canary denial failed: {rows}')
 else:
  complete=next((v for v in values if isinstance(v,dict) and v.get('phase9aDriverComplete')),None)
  if complete is None:raise RuntimeError(f'RPC driver did not complete {label}; tail={lines[-10:]}')
 return {'label':label,'target':target,'launch_wrapper_sha256':sha(Path(target[0])),'trace_tree_sha256':canonical._tree_digest(trace),'syscall_export_sha256':sha(xml),'target_output_sha256':sha(output),'observation':observation,'completion':complete if not expect_network else {'denied':True},'export_stderr':export.stderr.splitlines()}
def prepare_state(state:Path,hook:Path,config:dict):
 state.mkdir(parents=True);(state/'home').mkdir();(state/'config.yaml').write_text(json.dumps(config,sort_keys=True)+'\n');(state/'egress-attempts.jsonl').write_text('');os.chmod(state/'egress-attempts.jsonl',0o600)
def main()->int:
 ap=argparse.ArgumentParser();ap.add_argument('--bridge-2-0-14',type=Path,required=True);ap.add_argument('--bridge-2-0-10',type=Path,required=True);ap.add_argument('--driver',type=Path,required=True);ap.add_argument('--profile',type=Path,required=True);ap.add_argument('--canary',type=Path,required=True);ap.add_argument('--x86-manifest',type=Path,required=True);ap.add_argument('--arm-manifest',type=Path,required=True);ap.add_argument('--output-dir',type=Path,required=True);a=ap.parse_args()
 if a.output_dir.exists():raise SystemExit('output must not exist')
 if os.uname().machine!='arm64' or subprocess.run(['/usr/sbin/sysctl','-in','sysctl.proc_translated'],capture_output=True,text=True).stdout.strip() not in ('','0'):raise RuntimeError('native arm64/non-Rosetta required')
 sip=subprocess.run(['/usr/bin/csrutil','status'],capture_output=True,text=True,check=True).stdout
 if 'enabled' not in sip.lower():raise RuntimeError('SIP must be enabled')
 from harness_manager.memos_platform_attestation import attest_manifest
 attestation=attest_manifest(a.arm_manifest,'darwin-arm64',a.x86_manifest)
 out=a.output_dir;out.mkdir();traces=out/'traces';traces.mkdir();work=out/'work';work.mkdir();hook=work/'tripwire.cjs';hook.write_text(canonical.TRIPWIRE)
 arm_python=Path('/opt/homebrew/bin/python3');canary_target=[str(arm_python),str(a.canary)];direct=trace_process('direct-canary',canary_target,traces,a.profile,True);child=trace_process('child-boundary-canary',[*canary_target,'--spawn-child'],traces,a.profile,True,True)
 fresh=work/'fresh';prepare_state(fresh,hook,canonical._config()); env=lambda state:{'HOME':str(state/'home'),'MEMOS_HOME':str(state),'MEMOS_CONFIG_FILE':str(state/'config.yaml'),'MEMOS_EGRESS_EVIDENCE':str(state/'egress-attempts.jsonl'),'NODE_OPTIONS':f'--require={hook}'}
 def target(bridge,state,label,mode='full'):return ['/usr/bin/env',*[f'{k}={v}' for k,v in env(state).items()],str(ARM_NODE),str(a.driver),str(bridge),label,mode]
 fresh_r=trace_process('fresh-2.0.14',target(a.bridge_2_0_14,fresh,'fresh-2.0.14'),traces,a.profile)
 old=work/'old-source';prepare_state(old,hook,canonical._legacy_2010_config());old_r=trace_process('source-2.0.10',target(a.bridge_2_0_10,old,'source-2.0.10','seed'),traces,a.profile)
 states=[]
 for label in ('copied-2.0.10-opened-by-2.0.14','restored-2.0.10-opened-by-2.0.14'):
  state=work/label
  if label.startswith('copied'):shutil.copytree(old,state,ignore=shutil.ignore_patterns('egress-attempts.jsonl'))
  else:
   archive=Path(shutil.make_archive(str(work/'legacy-seed-backup'),'zip',root_dir=old));state.mkdir();shutil.unpack_archive(archive,state)
  (state/'egress-attempts.jsonl').write_text('');(state/'config.yaml').write_text(json.dumps(canonical._config(),sort_keys=True)+'\n');states.append(trace_process(label,target(a.bridge_2_0_14,state,label),traces,a.profile))
 candidate=[fresh_r,*states]
 attempts={p.name:p.read_text().splitlines() for p in work.rglob('egress-attempts.jsonl') if p.read_text().strip()}
 passed=not attempts and all(not run['observation']['network_attempts'] for run in [old_r,*candidate])
 evidence={'schema':'agentic.memory.phase9a-arm64-xctrace.v1','passed':passed,'attestation':attestation,'scope':{'legacy_2_0_10':'schema seed, health, and session lifecycle only','legacy_retrieval_used_as_qualification_evidence':False,'candidate_retrieval':'2.0.14 lexical only'},'host':{'machine':os.uname().machine,'sip':sip.strip(),'node':subprocess.run([ARM_NODE,'--version'],capture_output=True,text=True).stdout.strip(),'xctrace':subprocess.run(['/usr/bin/arch','-arm64','/usr/bin/xcrun','xctrace','version'],capture_output=True,text=True).stdout.strip()},'boundaries':{'controller':{'executable':sys.executable,'sha256':sha(Path(sys.executable)),'script_sha256':sha(Path(__file__))},'sandbox_profile_sha256':sha(a.profile),'arm_python_sha256':sha(arm_python),'arm_node_sha256':sha(ARM_NODE),'rpc_driver_sha256':sha(a.driver),'canary_sha256':sha(a.canary),'bridge_2_0_10_sha256':sha(a.bridge_2_0_10),'bridge_2_0_14_sha256':sha(a.bridge_2_0_14),'tripwire_sha256':sha(hook),'xctrace':'Apple System Call Trace','target_launches':[direct,child,old_r,*candidate]},'candidate_states':candidate,'application_attempts':attempts,'durable_evidence_policy':'retain normalized evidence.json and checksums only; raw trace/XML stay temporary and are not committed','r8_run':False,'deployed_state':'unchanged/off'}
 (out/'evidence.json').write_text(json.dumps(evidence,indent=2,sort_keys=True)+'\n');(out/'evidence.json.sha256').write_text(f'{sha(out/"evidence.json")}  evidence.json\n');print(json.dumps({'passed':passed,'output':str(out)},sort_keys=True));return 0 if passed else 1
if __name__=='__main__':raise SystemExit(main())
