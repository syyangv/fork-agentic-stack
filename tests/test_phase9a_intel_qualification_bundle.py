from __future__ import annotations
import ast,importlib.util,json,py_compile
from pathlib import Path
import pytest
ROOT=Path(__file__).resolve().parents[1]
TEMPLATES=ROOT/'tools/phase9a_intel_qualification/templates'
def load_parser():
 spec=importlib.util.spec_from_file_location('intel_parser',TEMPLATES/'parse_syscalls.py'); module=importlib.util.module_from_spec(spec); assert spec.loader; spec.loader.exec_module(module); return module
def write_xml(path:Path,calls:list[tuple[str,list[str],str]])->None:
 rows=[]
 for i,(name,args,errno) in enumerate(calls,1):
  argv=''.join(f'<syscall-arg fmt="{arg}"/>' for arg in args)
  rows.append(f'<row><syscall fmt="{name}"/>{argv}<syscall-return fmt="0x0"/><syscall-return fmt="{errno}"/></row>')
 path.write_text('<?xml version="1.0"?><trace-query-result><node><schema name="syscall"/>'+''.join(rows)+'</node></trace-query-result>')
def test_parser_accepts_denied_direct_canary(tmp_path):
 parser=load_parser(); xml=tmp_path/'calls.xml'; write_xml(xml,[('socket',['0x2','0x1','0x0'],'0x0'),('connect',['0x3'],'0x1'),('sendto',['0x4'],'0x1')]); result=parser.parse_rows(xml); parser.validate(result,'direct-canary')
def test_parser_rejects_empty_child_coverage(tmp_path):
 parser=load_parser(); xml=tmp_path/'calls.xml'; write_xml(xml,[('open',['0x0'],'0x0')]); result=parser.parse_rows(xml)
 with pytest.raises(RuntimeError,match='coverage incomplete'): parser.validate(result,'child-canary')
def test_parser_rejects_any_qualification_network_attempt(tmp_path):
 parser=load_parser(); xml=tmp_path/'calls.xml'; write_xml(xml,[('connect',['0x3'],'0x1')]); result=parser.parse_rows(xml)
 with pytest.raises(RuntimeError,match='observed network syscalls'): parser.validate(result,'zero')
def test_bundle_templates_compile_and_encode_strict_gates():
 for name in ('parse_syscalls.py','network_canary.py','run_qualification.py'): py_compile.compile(str(TEMPLATES/name),doraise=True)
 runner=(TEMPLATES/'run_qualification.py').read_text(); readme=(TEMPLATES/'README.md').read_text(); profile=(TEMPLATES/'deny-network.sb').read_text()
 for required in ('native Intel macOS x86_64 required','SIP must remain enabled','child-canary','manifest mismatch','r8_run'):
  assert required in runner
 assert '(deny network*)' in profile
 assert 'does not authorize merge, deployment, activation, or R8' in readme
 schema=json.loads((TEMPLATES/'evidence-schema.json').read_text()); assert schema['properties']['r8_run']['const'] is False

def test_runner_uses_canonical_installer_order_and_traced_tmp_output():
 runner=(TEMPLATES/'run_qualification.py').read_text()
 tree=ast.parse(runner)
 calls=[node for node in ast.walk(tree) if isinstance(node,ast.Call) and isinstance(node.func,ast.Name) and node.func.id=='install_verified_tarball']
 assert len(calls)==1
 assert [ast.unparse(arg) for arg in calls[0].args]==['a14','code']
 assert "qualification=work/'tmp-qualification/memos-xctrace-qualification.json'" in runner
