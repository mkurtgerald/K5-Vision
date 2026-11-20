"""Measure a pinned non-pausing sampler on our own disposable Python child."""
from __future__ import annotations
import argparse
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

SAMPLER_SHA='5d79feb301ecdc89b9436b67503a51c112f1250723e33946a5e679b53eb946bf'
EXE_SHA='0890578ca20bf839d7f890bbcdb508a21cb9f1b71f99bf32be371c5e492a0b48'


class OwnedPythonChild:
    def __init__(self, process):
        self.process=process
        self.pid=process.pid

    def is_running(self):
        return self.process.poll() is None


def finish(process):
    if process is None:
        return True
    try:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=2)
        return process.poll() is not None
    except BaseException:
        return False


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--sampler',type=Path,required=True)
    parser.add_argument('--executable',type=Path,required=True)
    parser.add_argument('--baseline',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    assert os.name=='nt' and os.environ.get('GITHUB_ACTIONS')=='true' and os.environ.get('RUNNER_ENVIRONMENT')=='github-hosted'
    assert hashlib.sha256(args.sampler.read_bytes()).hexdigest()==SAMPLER_SHA
    assert hashlib.sha256(args.executable.read_bytes()).hexdigest()==EXE_SHA
    spec=importlib.util.spec_from_file_location('pinned_startup_sampler',args.sampler)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    executable=module.prepare_executable(args.executable)
    index=module.public_index(args.baseline.parent)
    version=None
    version_process=child=None
    baseline_directory=None
    result={'schema_version':1,'scope':'sampler-startup-preflight-only','sampler_sha256':SAMPLER_SHA,'executable_sha256':EXE_SHA,'request_timeout_changed':False,'clock_changes':False,'http_requests':0,'release_qualification':False,'observations':[],'cleanup_complete':False,'baseline_capture_qualified':False}
    try:
        began=time.monotonic()
        version_process=subprocess.Popen([str(executable),'--version'],stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,creationflags=subprocess.CREATE_NO_WINDOW)
        try:
            version_raw,_=version_process.communicate(timeout=5)
        except BaseException:
            result['version_outcome']='timeout_or_failure'
            return 1
        result['version_elapsed_ms']=round((time.monotonic()-began)*1000)
        version=version_raw.strip()==b'py-spy 0.4.3' and version_process.returncode==0
        result['version_verified']=version
        if not version:
            return 1
        with tempfile.TemporaryDirectory(prefix='K5 sampler owned baseline ',dir=os.environ['RUNNER_TEMP']) as directory:
            baseline_directory=Path(directory)
            ready=Path(directory)/'ready'
            child=subprocess.Popen([sys.executable,'-I',str(args.baseline),str(ready)],stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,creationflags=subprocess.CREATE_NO_WINDOW)
            owned=OwnedPythonChild(child)
            wait_until=time.monotonic()+5
            while not ready.is_file() and time.monotonic()<wait_until and owned.is_running():
                time.sleep(0.01)
            result['owned_python_ready']=ready.is_file() and ready.read_bytes()==b'owned-baseline-ready\n' and owned.is_running()
            if not result['owned_python_ready']:
                return 1
            for attempt in [1,2,3]:
                began=time.monotonic()
                sample=module.sample_once(executable,owned,index)
                captured=sample['outcome']=='projected' and any(frame['file']=='clock_sampler_baseline.py' and frame['function']=='wait_for_owned_probe' for thread in sample['threads'] for frame in thread['frames'])
                result['observations'].append({'attempt':attempt,'cap_seconds':0.8,'elapsed_ms':round((time.monotonic()-began)*1000),'sample_outcome':sample['outcome'],'owned_public_frame_captured':captured,'baseline_stacks_discarded':True})
            result['baseline_capture_qualified']=all(x['owned_public_frame_captured'] for x in result['observations'])
    except BaseException as error:
        result['baseline_capture_qualified']=False
        result['failure_kind']=type(error).__name__ if type(error).__name__ in {'AssertionError','OSError','TimeoutExpired','ValueError'} else 'other'
        return 1
    finally:
        child_finished=finish(child)
        version_finished=finish(version_process)
        try:
            temporary_tree_removed=baseline_directory is None or not baseline_directory.exists()
        except BaseException:
            temporary_tree_removed=False
        result['cleanup_complete']=child_finished and version_finished and temporary_tree_removed and all(x['sample_outcome']!='sampler_cleanup_incomplete' for x in result['observations'])
        result['baseline_capture_qualified']=result['baseline_capture_qualified'] and result['cleanup_complete']
        args.output.write_text(json.dumps(result,indent=2)+'\n',encoding='utf-8')
        print(json.dumps(result,indent=2))
    return 0 if result['baseline_capture_qualified'] else 1


if __name__=='__main__':
    raise SystemExit(main())
