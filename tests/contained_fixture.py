"""Credentialless in-namespace model fixture; deliberately not a production proxy."""
import http.server
import json
import runpy
import sys
import threading


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def do_POST(self):
        length = int(self.headers.get('Content-Length', '0'))
        request = json.loads(self.rfile.read(length))
        with open('/home/agent/requests.jsonl', 'a') as out:
            out.write(json.dumps({'authorization': self.headers.get('Authorization'), 'request': request}) + '\n')
        messages = request.get('messages', [])
        has_tool_result = any(m.get('role') == 'tool' for m in messages)
        if not has_tool_result:
            # Model-requested malicious tool calls: test the actual Hermes dispatcher, through
            # both toolsets the seat gets (#16): the shell, and the file tool's own reader.
            host_paths = json.load(open('/home/agent/host-paths.json'))
            # Traversal: the same host paths reached through /proc's view of a root.
            routed = [prefix + path for path in host_paths
                      for prefix in ('/proc/self/root', '/proc/1/root')]
            command = ('cat ' + ' '.join(host_paths + routed) + '; '
                       'git credential fill </dev/null; cargo test --offline')
            calls = [('call_host_read', 'terminal', {'command': command})]
            calls += [(f'call_file_read_{i}', 'read_file', {'path': path})
                      for i, path in enumerate(host_paths + routed)]
            message = {'role': 'assistant', 'content': None, 'tool_calls': [{
                'id': call_id, 'type': 'function', 'function': {
                    'name': name, 'arguments': json.dumps(arguments)}}
                for call_id, name, arguments in calls]}
            finish = 'tool_calls'
        else:
            message = {'role': 'assistant', 'content': 'FIXTURE_DONE'}
            finish = 'stop'
        payload = {'id': 'fixture', 'object': 'chat.completion', 'created': 1,
                   'model': 'fixture-model', 'choices': [{'index': 0, 'message': message,
                                                        'finish_reason': finish}],
                   'usage': {'prompt_tokens': 10, 'completion_tokens': 10, 'total_tokens': 20}}
        if request.get('stream'):
            delta = {'role': 'assistant', 'content': message.get('content')}
            if 'tool_calls' in message:
                delta['tool_calls'] = [dict(index=i, id=call['id'], type='function',
                                            function=call['function'])
                                       for i, call in enumerate(message['tool_calls'])]
            chunk = {'id': 'fixture', 'object': 'chat.completion.chunk',
                     'created': 1, 'model': 'fixture-model',
                     'choices': [{'index': 0, 'delta': delta, 'finish_reason': None}]}
            end = {**chunk, 'choices': [{'index': 0, 'delta': {}, 'finish_reason': finish}]}
            data = (''.join('data: ' + json.dumps(part) + '\n\n' for part in (chunk, end))
                    + 'data: [DONE]\n\n').encode()
            content_type = 'text/event-stream'
        else:
            data = json.dumps(payload).encode()
            content_type = 'application/json'
        self.send_response(200)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)


server = http.server.ThreadingHTTPServer(('127.0.0.1', 18761), Handler)
threading.Thread(target=server.serve_forever, daemon=True).start()
sys.argv = ['/opt/venv/bin/hermes', 'chat', '--query-file', '/home/agent/query.txt',
            '--oneshot', '-Q', '--provider', 'custom', '-m', 'fixture-model',
            '-t', 'terminal,file', '--ignore-rules', '--max-turns', '3', '--run-budget', '90']
runpy.run_path('/opt/venv/bin/hermes', run_name='__main__')
