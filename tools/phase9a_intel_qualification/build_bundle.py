#!/usr/bin/env python3
from __future__ import annotations
import argparse,hashlib,json,shutil,subprocess,tarfile,tempfile
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2]
EXPECTED_2014='575af9121f86ad3eacfc6aca9d8a3e3e0856b2caa91a7d537a3c37ad3ee43907'
EXPECTED_2010='8fc583236c088694854a6e430d0612209812c938d2eca1ca630c381c9d4b171f'
def sha(p:Path)->str:return hashlib.sha256(p.read_bytes()).hexdigest()
def copy(src:Path,dst:Path)->None: dst.parent.mkdir(parents=True,exist_ok=True); shutil.copy2(src,dst)
def build(a14:Path,a10:Path,out:Path)->tuple[Path,Path]:
 if sha(a14)!=EXPECTED_2014 or sha(a10)!=EXPECTED_2010: raise RuntimeError('pinned artifact SHA-256 mismatch')
 out.mkdir(parents=True,exist_ok=True); bundle=out/'phase9a-intel-qualification-v1'
 if bundle.exists(): raise RuntimeError(f'refusing existing bundle directory: {bundle}')
 shutil.copytree(Path(__file__).parent/'templates',bundle,ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
 copy(a14,bundle/'artifacts/memtensor-memos-local-plugin-2.0.14.tgz'); copy(a10,bundle/'artifacts/memos-local-plugin-2.0.10.tgz')
 for rel in ('harness_manager/__init__.py','harness_manager/memos_install.py','harness_manager/assets/memos-2.0.10/package.json','harness_manager/assets/memos-2.0.10/package-lock.json','harness_manager/assets/memos-2.0.14/package.json','harness_manager/assets/memos-2.0.14/package-lock.json','harness_manager/assets/memos-2.0.14/package-lock.lexical.json','scripts/qualify_memos_zero_egress.py'):
  copy(ROOT/rel,bundle/'source'/rel)
 for name in ('__init__.py','_core.py','contracts.py','memos_journal.py','memos_runtime.py'):
  copy(ROOT/'.agent/memory/orchestration'/name,bundle/'source/.agent/memory/orchestration'/name)
 commit=subprocess.run(['git','rev-parse','HEAD'],cwd=ROOT,text=True,capture_output=True,check=True).stdout.strip()
 attested={p.relative_to(bundle).as_posix():sha(p) for p in sorted((bundle/'source').rglob('*')) if p.is_file()}
 (bundle/'source-attestation.json').write_text(json.dumps({'schema':'agentic.memory.phase9a-intel-bundle-source.v1','source_base_commit':commit,'source_identity':'all bundled source files are authoritative by their per-file SHA-256 below','decision':'A: preserve strict evidence using native Intel x86_64 macOS','files':attested},indent=2,sort_keys=True)+'\n')
 files=sorted(p for p in bundle.rglob('*') if p.is_file() and p.name!='SHA256SUMS')
 (bundle/'SHA256SUMS').write_text(''.join(f'{sha(p)}  {p.relative_to(bundle).as_posix()}\n' for p in files))
 archive=out/'phase9a-intel-qualification-v1.tar.gz'
 with tarfile.open(archive,'w:gz') as tf: tf.add(bundle,arcname=bundle.name,recursive=True)
 check=out/(archive.name+'.sha256'); check.write_text(f'{sha(archive)}  {archive.name}\n')
 return archive,check
def main()->int:
 ap=argparse.ArgumentParser(); ap.add_argument('--artifact-2-0-14',type=Path,required=True); ap.add_argument('--artifact-2-0-10',type=Path,required=True); ap.add_argument('--output-dir',type=Path,required=True); args=ap.parse_args(); archive,check=build(args.artifact_2_0_14.resolve(strict=True),args.artifact_2_0_10.resolve(strict=True),args.output_dir.resolve()); print(json.dumps({'archive':str(archive),'sha256':sha(archive),'checksum_file':str(check)},sort_keys=True)); return 0
if __name__=='__main__': raise SystemExit(main())
