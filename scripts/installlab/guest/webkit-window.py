"""A minimal WebKitGTK app, for the Linux app's control runs: what GTK, WebKit and
Mesa keep in the home folder for any such app's window, so it isn't charged to
the app under test.

Usage: python3 webkit-window.py <seconds>
"""

import shutil
import sys
import tempfile

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("WebKit2", "4.1")
from gi.repository import GLib, Gtk, WebKit2  # noqa: E402

# A page like the app's: a terminal pane that keeps scrolling new output.
PAGE = """<body style='margin:0'><pre id=t style='height:100vh;overflow:auto;margin:0'></pre>
<script>let n=0;setInterval(()=>{const t=document.getElementById('t');
t.textContent+='line '+(n++)+' downloading package '+Math.random()+'\\n';
t.scrollTop=t.scrollHeight},20)</script></body>"""

# Its own data folders, removed afterwards: only what lives outside them remains.
data = tempfile.mkdtemp(prefix="webkit-control-")
manager = WebKit2.WebsiteDataManager(
    base_data_directory=data, base_cache_directory=data
)
view = WebKit2.WebView.new_with_context(
    WebKit2.WebContext.new_with_website_data_manager(manager)
)
view.load_html(PAGE, None)
window = Gtk.Window(title="WebKitGTK control window")
window.set_default_size(800, 600)
window.add(view)
window.connect("destroy", Gtk.main_quit)
window.show_all()
GLib.timeout_add_seconds(int(sys.argv[1]), window.destroy)
Gtk.main()
shutil.rmtree(data, ignore_errors=True)
