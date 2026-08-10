"""Two-root attestation for reviewed architecture-specific MemOS distributions."""
from __future__ import annotations
import hashlib,json
from pathlib import Path
from typing import Any
PLATFORM_VARIANT_PATHS=frozenset({'node_modules/better-sqlite3/build/Release/better_sqlite3.node','node_modules/esbuild/bin/esbuild'})
COMMON_MANIFEST_SHA256='9f7b082daee46beb4b689c34935b7c6c933fa49277d275ad7d3d775ea1d3f673'
PLATFORM_PINS={
 'darwin-x86_64':{'manifest':'dc0aae1417698ed4343895b292fb2f6ac1bcef4820eff6eb46875405b1ed73d9','native':{'node_modules/better-sqlite3/build/Release/better_sqlite3.node':('af38de1a26e51a3bee7ec05ec9810e9a39c5da41c31b3766ba5ca37f94bd8966',1925608),'node_modules/esbuild/bin/esbuild':('dd53ccf32f9b5b3ab30d41388ef1fc8f81c44ca57ee7a32a7364a1753308d009',11630864)}},
 'darwin-arm64':{'manifest':'26a6e3eba5ba2cef555a80f27643f74687fb855e4d2401c592539bb591d944c9','native':{'node_modules/better-sqlite3/build/Release/better_sqlite3.node':('1946ab352978b5b3493f99080dc04ed6d0a4157c511c7139cbf50b3d44d8d560',1931552),'node_modules/esbuild/bin/esbuild':('e2dc9a52440a2a34f09434a2f4843cb1e30f84e40dcf238976ec61ef8cd7f36a',10573778)}},
}
def digest(payload:bytes)->str:return hashlib.sha256(payload).hexdigest()
def attest_manifest(path:Path,platform_key:str,reference_path:Path)->dict[str,Any]:
 manifest=json.loads(path.read_text()); reference=json.loads(reference_path.read_text()); pin=PLATFORM_PINS.get(platform_key)
 if pin is None: raise RuntimeError(f'unapproved platform: {platform_key}')
 if set(manifest)!=set(reference): raise RuntimeError('platform manifest path set mismatch')
 differences={key for key in manifest if manifest[key]!=reference[key]}
 if differences!=PLATFORM_VARIANT_PATHS: raise RuntimeError(f'unapproved platform differences: {sorted(differences)}')
 common={key:value for key,value in manifest.items() if key not in PLATFORM_VARIANT_PATHS}
 common_sha=digest(json.dumps(common,separators=(',',':'),sort_keys=True).encode())
 if common_sha!=COMMON_MANIFEST_SHA256: raise RuntimeError('common manifest mismatch')
 full_sha=digest(path.read_bytes())
 if full_sha!=pin['manifest']: raise RuntimeError('platform manifest mismatch')
 for key,(expected_sha,expected_size) in pin['native'].items():
  row=manifest[key]
  if row!={'type':'file','sha256':expected_sha,'size':expected_size}: raise RuntimeError(f'native leaf mismatch: {key}')
 return {'schema':'agentic.memory.phase9a-runtime-attestation.v2','platform':platform_key,'full_manifest_sha256':full_sha,'common_manifest_sha256':common_sha,'platform_variant_paths':sorted(differences),'native':pin['native']}
