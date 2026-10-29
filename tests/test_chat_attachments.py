import base64
from io import BytesIO
import unittest
import zipfile
from PIL import Image
from chat_attachments import attachment, prepare_messages


def file(name,raw):return {'name':name,'data':base64.b64encode(raw).decode()}


class AttachmentTests(unittest.TestCase):
    def test_images_pdf_and_text_for_three_protocols(self):
        stream=BytesIO();Image.new('RGB',(4,4),'green').save(stream,'PNG')
        files=[file('图片.png',stream.getvalue()),file('报告.pdf',b'%PDF-1.7\nfixture'),file('说明.txt','附件正文'.encode())]
        for protocol in ('openai','responses','anthropic'):
            result=prepare_messages([{'role':'user','content':'帮我分析','attachments':files}],protocol)
            blocks=result[0]['content']
            self.assertIn('附件正文',blocks[0]['text'])
            self.assertEqual(blocks[1]['type'],{'openai':'image_url','responses':'input_image','anthropic':'image'}[protocol])
            self.assertEqual(blocks[2]['type'],{'openai':'file','responses':'input_file','anthropic':'document'}[protocol])
        with zipfile.ZipFile(stream:=BytesIO(),'w') as archive:
            archive.writestr('word/document.xml','<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:p><w:r><w:t>文档正文</w:t></w:r></w:p></w:document>')
        self.assertIn('文档正文',attachment(file('笔记.docx',stream.getvalue()))['text'])

    def test_invalid_files_and_limits(self):
        for item in [file('fake.png',b'not an image'),file('fake.pdf',b'not PDF'),file('x.exe',b'test'),file('x.txt',b'\x00'),{'name':'x.txt','data':'%%%'}]:
            with self.assertRaises(ValueError):attachment(item)
        for messages in [[],[{'role':'system','content':'x'}],[{'role':'user','content':'','attachments':[]}],
                         [{'role':'assistant','content':'x','attachments':[file('x.txt',b'x')]}],
                         [{'role':'user','content':'x','attachments':[file('x.txt',b'x')]*7}]]:
            with self.assertRaises(ValueError):prepare_messages(messages,'openai')
        self.assertEqual(prepare_messages([{'role':'user','content':'','attachments':[file('x.txt',b'hello')]}],'openai')[0]['role'],'user')
