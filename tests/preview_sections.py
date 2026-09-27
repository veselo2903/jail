"""Isolated local server for browser_sections.py, never uses the working database."""
import os,sys,tempfile
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
folder=Path(tempfile.mkdtemp(prefix='jail-sections-preview-'))
os.environ['JAIL_DB_PATH']=str(folder/'preview.db')
os.environ['JAIL_SECRET_KEY']='isolated-sections-browser'
from jail.app import app
app.run(host='127.0.0.1',port=18769,debug=False)
