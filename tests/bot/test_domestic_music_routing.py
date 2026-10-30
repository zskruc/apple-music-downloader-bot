import ast,os,unittest
from pathlib import Path
from unittest.mock import patch
class DomesticMusicRoutingTests(unittest.TestCase):
 def test_missing_music_search_proxy_does_not_inherit_telegram_proxy(self):
  source=Path(__file__).resolve().parents[2]/'bot'/'ingest_bot.py'
  tree=ast.parse(source.read_text())
  assignment=next(n for n in tree.body if isinstance(n,ast.Assign) and any(isinstance(t,ast.Name) and t.id=='SEARCH_PROXY_URL' for t in n.targets))
  with patch.dict(os.environ,{'TELEGRAM_PROXY_URL':'http://telegram:7891'},clear=True):
   namespace={'env':lambda key,default='':os.environ.get(key,default),'TELEGRAM_PROXY_URL':os.environ['TELEGRAM_PROXY_URL']}
   exec(compile(ast.Module(body=[assignment],type_ignores=[]),str(source),'exec'),namespace)
   self.assertEqual(namespace['SEARCH_PROXY_URL'],'')
 def test_music_clients_disable_environment_proxy(self):
  root=Path('/app') if Path('/app/lx_fallback.py').exists() else Path(__file__).resolve().parents[2]/'bot'
  for name in ['lx_fallback.py','sq_music.py']:
   with self.subTest(client=name):
    tree=ast.parse((root/name).read_text())
    handlers=[n for n in ast.walk(tree) if isinstance(n,ast.Call) and isinstance(n.func,ast.Attribute) and n.func.attr=='ProxyHandler']
    self.assertTrue(any(len(n.args)==1 and isinstance(n.args[0],ast.Dict) and not n.args[0].keys for n in handlers))
