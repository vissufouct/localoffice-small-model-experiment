"""Build the pinned worker using the project-local SDK; no model downloads."""
import argparse
import os
from pathlib import Path
import subprocess

ROOT=Path(__file__).resolve().parents[1]
parser=argparse.ArgumentParser()
parser.add_argument('--restore',action='store_true',help='Restore packages from the committed NuGet lockfile first')
args=parser.parse_args()
dotnet=ROOT/'.runtime/dotnet/dotnet'
if not dotnet.exists():raise SystemExit('Install the approved project-local .NET SDK first; see docs/benchmarks/dotnet-install.json.')
worker=ROOT/'workers/openxml'
env={**os.environ,'DOTNET_CLI_TELEMETRY_OPTOUT':'1','DOTNET_GENERATE_ASPNET_CERTIFICATE':'false'}
if args.restore:
    subprocess.run([str(dotnet),'restore','LocalOffice.OpenXml.csproj','--locked-mode','--packages',str(ROOT/'.runtime/nuget')],cwd=worker,env=env,check=True)
subprocess.run([str(dotnet),'build','LocalOffice.OpenXml.csproj','--no-restore','--nologo'],cwd=worker,env=env,check=True)
