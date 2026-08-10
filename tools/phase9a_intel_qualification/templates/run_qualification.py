#!/usr/bin/env python3
from __future__ import annotations
import hashlib,json,os,platform,shutil,subprocess,sys,tempfile
from pathlib import Path

ROOT=Path(__file__).resolve().parent
EXPECTED={
 'artifact_2014':'575af9121f86ad3eacfc6aca9d8a3e3e0856b2caa91a7d537a3c37ad3ee43907',
 'artifact_2010':'8fc583236c088694854a6e430d0612209812c938d2eca1ca630c381c9d4b171f',
 'manifest_2014':'dc0aae1417698ed4343895b292fb2f6ac1bcef4820eff6eb46875405b1ed73d9',
 'bridge_2010':'fc58eb07a35b6fec9f74646f98dca90ac5576d43ed2d87cad211241efc8a8ad7',
}
def sha(path:Path)->str:return hashlib.sha256(path.read_bytes()).hexdigest()
def run(*args,**kwargs):return subprocess.run(args,check=True,text=True,capture_output=True,**kwargs)
def verify_bundle()->None:
 for line in (ROOT/'SHA256SUMS').read_text().splitlines():
  expected,name=line.split('  ',1)
  if name=='SHA256SUMS': raise RuntimeError('recursive checksum entry rejected')
  path=ROOT/name
  if not path.is_file() or sha(path)!=expected: raise RuntimeError(f'bundle checksum mismatch: {name}')
def attest()->dict:
 if platform.system()!='Darwin' or platform.machine()!='x86_64': raise RuntimeError('native Intel macOS x86_64 required')
 translated_result=subprocess.run(['/usr/sbin/sysctl','-in','sysctl.proc_translated'],text=True,capture_output=True)
 translated=translated_result.stdout.strip() if translated_result.returncode==0 else '0'
 if translated not in ('0',''): raise RuntimeError('Rosetta-translated process rejected')
 sip=run('/usr/bin/csrutil','status').stdout.strip()
 if 'enabled' not in sip.lower(): raise RuntimeError(f'SIP must remain enabled: {sip}')
 node=shutil.which('node'); npm=shutil.which('npm')
 if not node or not npm: raise RuntimeError('Node and npm required')
 node_file=run('/usr/bin/file',node).stdout.strip()
 if 'x86_64' not in node_file: raise RuntimeError(f'x86_64 Node required: {node_file}')
 xctrace=run('/usr/bin/xcrun','xctrace','version').stdout.strip()
 if xctrace!='xctrace version 16.0 (16C5032a)': raise RuntimeError(f'xctrace version mismatch: {xctrace}')
 node_version=run(node,'--version').stdout.strip(); npm_version=run(npm,'--version').stdout.strip()
 if node_version!='v22.22.2' or npm_version!='10.9.7': raise RuntimeError(f'Node/npm tool mismatch: {node_version} / {npm_version}')
 os_version=run('/usr/bin/sw_vers','-productVersion').stdout.strip(); os_build=run('/usr/bin/sw_vers','-buildVersion').stdout.strip()
 if (os_version,os_build)!=('14.5','23F79'): raise RuntimeError(f'macOS mismatch: {os_version} ({os_build})')
 instruments=run('/usr/bin/xcrun','xctrace','list','instruments').stdout
 if 'System Call Trace' not in instruments: raise RuntimeError('System Call Trace unavailable')
 return {'schema':'agentic.memory.phase9a-intel-host-attestation.v1','uname':run('/usr/bin/uname','-a').stdout.strip(),'os':run('/usr/bin/sw_vers').stdout.splitlines(),'hardware_model':run('/usr/sbin/sysctl','-n','hw.model').stdout.strip(),'cpu':run('/usr/sbin/sysctl','-n','machdep.cpu.brand_string').stdout.strip(),'sip':sip,'translated':translated,'node':node_version,'node_file':node_file,'npm':npm_version,'xctrace':xctrace}
def install(work:Path)->tuple[Path,Path]:
 a14=ROOT/'artifacts/memtensor-memos-local-plugin-2.0.14.tgz'; a10=ROOT/'artifacts/memos-local-plugin-2.0.10.tgz'
 if sha(a14)!=EXPECTED['artifact_2014'] or sha(a10)!=EXPECTED['artifact_2010']: raise RuntimeError('artifact hash mismatch')
 sys.path.insert(0,str(ROOT/'source'))
 from harness_manager.memos_install import install_verified_tarball
 code=work/'code'; result=install_verified_tarball(a14,code)
 manifest=result.plugin_dir/'.agentic-stack-files.json'
 if sha(manifest)!=EXPECTED['manifest_2014']: raise RuntimeError(f'Intel immutable manifest mismatch: {sha(manifest)}')
 old=work/'runtime-2010'; old.mkdir()
 for name in ('package.json','package-lock.json'): shutil.copy2(ROOT/f'source/harness_manager/assets/memos-2.0.10/{name}',old/name)
 shutil.copy2(a10,old/'plugin.tgz')
 clean={'PATH':os.environ.get('PATH','/usr/local/bin:/usr/bin:/bin'),'HOME':str(work/'npm-home'),'TMPDIR':str(work/'npm-tmp')}
 subprocess.run(['npm','ci','--prefix',str(old),'--omit=dev','--no-audit','--no-fund'],check=True,env=clean)
 bridge10=old/'node_modules/@memtensor/memos-local-plugin/dist/bridge.cjs'
 if sha(bridge10)!=EXPECTED['bridge_2010']: raise RuntimeError('2.0.10 bridge hash mismatch')
 return result.package_dir/'dist/bridge.cjs',bridge10
def trace(label:str,expect:str,out:Path,*command:str)->None:
 env={**os.environ,'PHASE9A_BUNDLE':str(ROOT),'PHASE9A_WORK':str(out.parent)}
 subprocess.run([str(ROOT/'trace_target.sh'),label,expect,str(out),'--',*command],check=True,env=env)
def main()->int:
 verify_bundle(); attestation=attest()
 work=Path(tempfile.mkdtemp(prefix='phase9a-intel-',dir='/private/tmp')); (work/'home-direct').mkdir(); (work/'tmp-direct').mkdir()
 evidence=work/'evidence'; traces=work/'traces'; evidence.mkdir(); traces.mkdir()
 (evidence/'host-attestation.json').write_text(json.dumps(attestation,indent=2,sort_keys=True)+'\n')
 trace('direct-canary','direct-canary',traces,sys.executable,str(ROOT/'network_canary.py'),'--mode','direct')
 trace('child-canary','child-canary',traces,sys.executable,str(ROOT/'network_canary.py'),'--mode','parent')
 bridge14,bridge10=install(work)
 qualification=work/'tmp-qualification/memos-xctrace-qualification.json'
 trace('qualification','zero',traces,sys.executable,str(ROOT/'source/scripts/qualify_memos_zero_egress.py'),'--bridge-2-0-14',str(bridge14),'--bridge-2-0-10',str(bridge10),'--output',str(qualification))
 result=json.loads(qualification.read_text())
 if not result.get('passed'): raise RuntimeError('application qualification failed')
 shutil.copy2(qualification,evidence/'qualification.json')
 for path in traces.iterdir():
  if path.suffix in {'.xml','.json','.stdout','.log'} and not path.name.endswith('.xctrace.log'):
   shutil.copy2(path,evidence/path.name)
 record={'schema':'agentic.memory.phase9a-intel-external-result.v1','passed':True,'r8_run':False,'deployed_state':'unchanged/off','manifest_2014':EXPECTED['manifest_2014'],'raw_trace_policy':'owner-local; target-only syscall XML imported'}
 (evidence/'result.json').write_text(json.dumps(record,indent=2,sort_keys=True)+'\n')
 files=sorted(p for p in evidence.iterdir() if p.is_file())
 (evidence/'SHA256SUMS').write_text(''.join(f'{sha(p)}  {p.name}\n' for p in files))
 print(json.dumps({'passed':True,'evidence':str(evidence),'work':str(work)},sort_keys=True)); return 0
if __name__=='__main__': raise SystemExit(main())
