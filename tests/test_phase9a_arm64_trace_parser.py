from __future__ import annotations
import importlib.util
from pathlib import Path
import pytest

ROOT=Path(__file__).resolve().parents[1]
SPEC=importlib.util.spec_from_file_location('arm_trace',ROOT/'scripts/qualify_memos_arm64_xctrace.py')
MODULE=importlib.util.module_from_spec(SPEC);assert SPEC.loader;SPEC.loader.exec_module(MODULE)

def xml(tmp_path:Path,rows:str)->Path:
 path=tmp_path/'trace.xml';path.write_text(f'<trace>{rows}</trace>');return path
def row(pid:str,call:str,args:str='',ret:str='0x0')->str:
 return f'<row><process fmt="{pid}"/><syscall fmt="{call}"/>{args}<syscall-return fmt="{ret}"/></row>'

def test_parser_rejects_decimal_inet_socket(tmp_path):
 path=xml(tmp_path,row('node (1)','socket','<syscall-arg fmt="2"/>') )
 with pytest.raises(RuntimeError,match='network attempts observed'):MODULE.parse_syscalls(path)

def test_parser_accepts_denied_direct_canary(tmp_path):
 rows=row('python (1)','socket','<syscall-arg fmt="AF_INET (2)"/>')+row('python (1)','connect','', '0x1')+row('python (1)','sendto','', '1')
 result=MODULE.parse_syscalls(xml(tmp_path,rows),expect_network=True)
 assert {item['call'] for item in result['network_attempts']}=={'socket','connect','sendto'}

def test_parser_fails_candidate_child_creation_and_models_blind_spot(tmp_path):
 path=xml(tmp_path,row('node (1)','fork'))
 with pytest.raises(RuntimeError,match='untraced child boundary'):MODULE.parse_syscalls(path)
 result=MODULE.parse_syscalls(path,expect_network=True,expect_child=True)
 assert result['descendant_trace_visibility'] is False

def test_parser_rejects_posix_spawn_variants(tmp_path):
 with pytest.raises(RuntimeError,match='untraced child boundary'):
  MODULE.parse_syscalls(xml(tmp_path,row('node (1)','posix_spawnattr_setflags')))
