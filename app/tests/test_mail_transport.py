import base64
import json
import os
import tempfile
import unittest
from email.message import EmailMessage
from pathlib import Path

try:
    from mail_transport import GmailTransport
except ModuleNotFoundError:
    GmailTransport = None


class GmailTransportTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(GmailTransport, 'standalone transport is not implemented')
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.creds = Path(self.tmp.name) / 'credentials.json'
        self.creds.write_text(json.dumps(dict(client_id='test-client', client_secret='test-secret', refresh_token='test-refresh')))
        self.creds.chmod(0o600)
        self.calls = []

    def message(self):
        m = EmailMessage()
        m['From'] = 'sender@example.com'
        m['To'] = 'recipient@example.com'
        m['Subject'] = 'Verified job'
        m['Message-ID'] = '<fixed-job@example.com>'
        m.set_content('Job link: https://example.com/jobs/1')
        m.add_attachment(b'%PDF-1.4\nTEST ONLY', maintype='application', subtype='pdf', filename='resume.pdf')
        return m

    def http(self, method, url, headers, body, timeout):
        self.calls.append((method, url, headers, body, timeout))
        if url == 'https://oauth2.googleapis.com/token':
            return 200, b'{"access_token":"test-access","token_type":"Bearer"}'
        return 200, b'{"id":"gmail-id","threadId":"gmail-thread"}'

    def adapter(self, http=None):
        return GmailTransport(self.creds, recipient='recipient@example.com', sender='sender@example.com', http=http or self.http)

    def test_exact_https_request_and_api_acceptance_is_not_delivery(self):
        raw = self.message().as_bytes()
        result = self.adapter().send(raw)
        self.assertEqual(result['state'], 'api_accepted')
        self.assertFalse(result['delivery_verified'])
        self.assertEqual(result['provider_message_id'], 'gmail-id')
        self.assertEqual(len(self.calls), 2)
        method, url, headers, body, timeout = self.calls[1]
        self.assertEqual((method, url), ('POST', 'https://gmail.googleapis.com/gmail/v1/users/me/messages/send'))
        self.assertEqual(headers['Authorization'], 'Bearer test-access')
        self.assertEqual(base64.urlsafe_b64decode(json.loads(body)['raw']), raw)
        self.assertEqual(timeout, 15)
        self.assertIn(b'grant_type=refresh_token', self.calls[0][3])
    def test_send_deadline_is_checked_after_token_refresh_before_mail_handoff(self):
        allowed=[True]
        def http(method,url,headers,body,timeout):
            result=self.http(method,url,headers,body,timeout)
            if url=='https://oauth2.googleapis.com/token':allowed[0]=False
            return result
        transport=GmailTransport(self.creds,recipient='recipient@example.com',sender='sender@example.com',http=http,send_allowed=lambda:allowed[0])
        result=transport.send(self.message().as_bytes());self.assertEqual(result['state'],'not_attempted');self.assertEqual(len(self.calls),1)
        self.assertEqual(self.calls[0][1],'https://oauth2.googleapis.com/token')

    def test_rejects_extra_envelope_headers_before_http(self):
        for header in ['Cc', 'Bcc', 'Resent-To', 'Reply-To', 'Sender', 'Return-Path', 'X-Original-To']:
            with self.subTest(header=header):
                m = self.message(); m[header] = 'attacker@example.com'
                with self.assertRaises(ValueError): self.adapter().send(m.as_bytes())
        self.assertEqual(self.calls, [])

    def test_rejects_wrong_multiple_display_or_duplicate_recipients(self):
        for to in ['attacker@example.com', 'recipient@example.com, attacker@example.com', 'Person <recipient@example.com>']:
            m = self.message(); m.replace_header('To', to)
            with self.assertRaises(ValueError): self.adapter().send(m.as_bytes())
        raw = self.message().as_bytes().replace(b'To: recipient@example.com', b'To: recipient@example.com\nTo: recipient@example.com')
        with self.assertRaises(ValueError): self.adapter().send(raw)
        self.assertEqual(self.calls, [])

    def test_rejects_other_sender_missing_id_and_extra_or_non_pdf_attachment(self):
        m = self.message(); m.replace_header('From', 'attacker@example.com')
        with self.assertRaises(ValueError): self.adapter().send(m.as_bytes())
        m = self.message(); del m['Message-ID']
        with self.assertRaises(ValueError): self.adapter().send(m.as_bytes())
        m = self.message(); m.add_attachment(b'extra', maintype='text', subtype='plain', filename='extra.txt')
        with self.assertRaises(ValueError): self.adapter().send(m.as_bytes())
        m = self.message(); list(m.iter_attachments())[0].set_payload('not a PDF')
        with self.assertRaises(ValueError): self.adapter().send(m.as_bytes())
        self.assertEqual(self.calls, [])

    def test_credentials_reject_public_file_symlink_and_writable_parent(self):
        self.creds.chmod(0o644)
        with self.assertRaises(ValueError): self.adapter().send(self.message().as_bytes())
        self.creds.chmod(0o600)
        link = self.creds.parent / 'link.json'; link.symlink_to(self.creds)
        with self.assertRaises((ValueError, OSError)):
            GmailTransport(link, recipient='recipient@example.com', sender='sender@example.com', http=self.http).send(self.message().as_bytes())
        self.creds.parent.chmod(0o777)
        try:
            with self.assertRaises(ValueError): self.adapter().send(self.message().as_bytes())
        finally: self.creds.parent.chmod(0o700)
        self.assertEqual(self.calls, [])

    def test_send_timeout_is_unknown_and_never_retried(self):
        def http(*args):
            if args[1] == 'https://oauth2.googleapis.com/token': return self.http(*args)
            self.calls.append(args)
            raise TimeoutError('sensitive server details')
        result = self.adapter(http).send(self.message().as_bytes())
        self.assertEqual(result['state'], 'unknown')
        self.assertFalse(result['delivery_verified'])
        self.assertEqual(len(self.calls), 2)
        self.assertNotIn('sensitive', json.dumps(result))

    def test_http_failures_and_malformed_success_never_claim_acceptance(self):
        for status, payload, expected in [(401,b'{}','rejected'), (429,b'{}','rejected'), (503,b'{}','unknown'), (200,b'{}','unknown'), (200,b'not-json','unknown')]:
            with self.subTest(status=status, payload=payload):
                self.calls = []
                def http(*args):
                    if args[1] == 'https://oauth2.googleapis.com/token': return self.http(*args)
                    self.calls.append(args); return status, payload
                result = self.adapter(http).send(self.message().as_bytes())
                self.assertEqual(result['state'], expected)
                self.assertEqual(len(self.calls), 2)

    def test_refresh_failure_does_not_attempt_send(self):
        def http(*args): self.calls.append(args); return 400, b'{"error":"invalid_grant"}'
        result = self.adapter(http).send(self.message().as_bytes())
        self.assertEqual(result['state'], 'not_attempted')
        self.assertEqual(len(self.calls), 1)

    def test_nested_address_header_and_malformed_mime_are_rejected(self):
        m = self.message(); list(m.iter_attachments())[0]['Bcc'] = 'attacker@example.com'
        with self.assertRaises(ValueError): self.adapter().send(m.as_bytes())
        with self.assertRaises(ValueError): self.adapter().send(b'To: recipient@example.com\n\nnot MIME')
        self.assertEqual(self.calls, [])

    def test_invalid_transfer_encodings_never_reach_http(self):
        for index, encoding, payload in [(1, 'base64', 'JVBERi0xLjQ=!!!'), (0, 'base64', 'SGVsbG8=!!!'), (0, 'x-unsupported', 'body'), (1, 'x-unsupported', '%PDF-1.4')]:
            with self.subTest(part=index, encoding=encoding):
                message = self.message(); part = list(message.iter_parts())[index]
                part.replace_header('Content-Transfer-Encoding', encoding)
                part.set_payload(payload)
                with self.assertRaises(ValueError): self.adapter().send(message.as_bytes())
        self.assertEqual(self.calls, [])

    def test_rate_limit_status_and_retry_after_are_propagated_without_body(self):
        def http(method,url,headers,body,timeout):
            if url=='https://oauth2.googleapis.com/token':return 200,b'{"access_token":"test-access"}',None
            return 429,b'private provider error','180'
        result=self.adapter(http=http).send(self.message().as_bytes())
        self.assertEqual(result['state'],'rejected');self.assertEqual(result['http_status'],429);self.assertEqual(result['retry_after'],'180')
        self.assertNotIn('private provider error',json.dumps(result))


if __name__ == '__main__': unittest.main()
