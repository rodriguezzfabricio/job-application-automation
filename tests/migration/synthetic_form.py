"""Loopback-only synthetic ATS; no external request, real résumé or notification."""
from http.server import BaseHTTPRequestHandler, HTTPServer
import json

PAGE='''<!doctype html><title>Synthetic application — migration check</title>
<h1>Synthetic application</h1><p>No employer receives this form.</p>
<form action="/submit" method="post" enctype="multipart/form-data">
<label>Name <input name="name" required></label>
<label>Saved factual answer <input name="answer" required></label>
<label>Résumé <input type="file" name="resume" accept="application/pdf" required></label>
<button type="submit">Submit synthetic application</button></form>'''

class Handler(BaseHTTPRequestHandler):
    submissions=0
    def log_message(self,*args): pass
    def do_GET(self):
        body=(json.dumps({'submissions':self.submissions}) if self.path=='/count' else PAGE).encode()
        self.send_response(200);self.end_headers();self.wfile.write(body)
    def do_POST(self):
        data=self.rfile.read(int(self.headers['Content-Length']))
        if b'%PDF-' not in data:
            self.send_response(422);self.end_headers();self.wfile.write(b'PDF required');return
        type(self).submissions+=1
        self.send_response(200);self.end_headers();self.wfile.write(b'<title>Confirmation</title><h1>Synthetic application received</h1><p>Confirmation SYNTHETIC-1</p>')

if __name__=='__main__': HTTPServer(('127.0.0.1',8767),Handler).serve_forever()
