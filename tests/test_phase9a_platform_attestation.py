from __future__ import annotations
import json
from pathlib import Path
import pytest
import harness_manager.memos_platform_attestation as attestation
from harness_manager.memos_platform_attestation import attest_manifest
ROOT=Path(__file__).resolve().parents[1]
EVIDENCE=ROOT/'docs/evidence/phase9a-memos-2.0.14/lexical-remediation-v1/arm64-native-v1'
X86=EVIDENCE/'darwin-x86_64-files-manifest.json'
ARM=EVIDENCE/'darwin-arm64-files-manifest.json'
def test_arm_manifest_attests_against_x86():
 result=attest_manifest(ARM,'darwin-arm64',X86)
 durable=json.loads((EVIDENCE/'zero-egress-summary.json').read_text())['attestation']
 assert json.loads(json.dumps(result))==durable
def test_common_mutation_fails(tmp_path):
 data=json.loads(ARM.read_text()); key=next(k for k in data if k not in result_paths()); data[key]={**data[key],'size':int(data[key].get('size',0))+1}; path=tmp_path/'manifest.json'; path.write_text(json.dumps(data,separators=(',',':'),sort_keys=True)+'\n')
 with pytest.raises(RuntimeError): attest_manifest(path,'darwin-arm64',X86)

def test_independent_trace_review_binds_durable_summary_and_verifier():
 summary=json.loads((EVIDENCE/'zero-egress-summary.json').read_text())
 review=json.loads((EVIDENCE/'independent-trace-review.json').read_text())
 expected={row['label']:{key:row[key] for key in ('trace_tree_sha256','syscall_export_sha256','target_output_sha256')} for row in summary['runs']}
 actual={row['label']:{key:row[key] for key in ('trace_tree_sha256','syscall_export_sha256','target_output_sha256')} for row in review['verification']['runs']}
 assert actual==expected
 assert review['verification']['all_raw_hashes_match'] is True
 assert review['verification']['all_normalized_observations_match'] is True
 assert review['verification']['verifier_sha256']==attestation.digest((ROOT/'scripts/qualify_memos_arm64_xctrace.py').read_bytes())
def result_paths(): return {'node_modules/better-sqlite3/build/Release/better_sqlite3.node','node_modules/esbuild/bin/esbuild'}

def test_portable_two_root_contract(monkeypatch,tmp_path):
 common={'package.json':{'type':'file','sha256':'a'*64,'size':7}}
 x86={**common,
      'node_modules/better-sqlite3/build/Release/better_sqlite3.node':{'type':'file','sha256':'b'*64,'size':11},
      'node_modules/esbuild/bin/esbuild':{'type':'file','sha256':'c'*64,'size':13}}
 arm={**common,
      'node_modules/better-sqlite3/build/Release/better_sqlite3.node':{'type':'file','sha256':'d'*64,'size':17},
      'node_modules/esbuild/bin/esbuild':{'type':'file','sha256':'e'*64,'size':19}}
 def write(name,value):
  path=tmp_path/name;path.write_text(json.dumps(value,separators=(',',':'),sort_keys=True));return path
 xp=write('x86.json',x86);ap=write('arm.json',arm)
 monkeypatch.setattr(attestation,'COMMON_MANIFEST_SHA256',attestation.digest(json.dumps(common,separators=(',',':'),sort_keys=True).encode()))
 monkeypatch.setitem(attestation.PLATFORM_PINS,'portable',{'manifest':attestation.digest(ap.read_bytes()),'native':{key:(arm[key]['sha256'],arm[key]['size']) for key in result_paths()}})
 assert attest_manifest(ap,'portable',xp)['platform_variant_paths']==sorted(result_paths())
 mutated=dict(arm);mutated['third.bin']={'type':'file','sha256':'f'*64,'size':1}
 with pytest.raises(RuntimeError,match='path set mismatch'):attest_manifest(write('extra.json',mutated),'portable',xp)
 mutated=json.loads(json.dumps(arm));mutated['package.json']['type']='symlink'
 with pytest.raises(RuntimeError,match='unapproved platform differences'):attest_manifest(write('type.json',mutated),'portable',xp)
