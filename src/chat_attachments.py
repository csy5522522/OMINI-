"""Bounded in-memory attachment validation and protocol-specific content blocks."""
import base64
import binascii
from io import BytesIO
from pathlib import PurePath
import warnings
import zipfile
from xml.etree import ElementTree
from PIL import Image

MAX_FILE = 5 * 1024 * 1024
MAX_TOTAL = 12 * 1024 * 1024
TEXT_TYPES = {'.txt', '.md', '.csv', '.json', '.log', '.py', '.js', '.ts', '.html', '.css'}


def attachment(value):
    if not isinstance(value, dict) or set(value) != {'name', 'data'}:
        raise ValueError('附件格式有误。')
    name, encoded = value['name'], value['data']
    if not isinstance(name, str) or not 1 <= len(name) <= 240 or any(ord(c) < 32 for c in name):
        raise ValueError('附件名称格式有误。')
    if not isinstance(encoded, str) or len(encoded) > (MAX_FILE + 2)//3*4:
        raise ValueError('每个附件最多 5 MB。')
    try: raw = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error): raise ValueError('附件数据有误。') from None
    if not raw or len(raw) > MAX_FILE: raise ValueError('请选择非空附件，每个最多 5 MB。')
    suffix = PurePath(name).suffix.lower()
    text = None; mime = None
    if suffix in {'.png', '.jpg', '.jpeg', '.webp'}:
        try:
            with warnings.catch_warnings():
                warnings.simplefilter('error', Image.DecompressionBombWarning)
                with Image.open(BytesIO(raw)) as img:
                    if img.width * img.height > 25_000_000 or img.format not in ('PNG','JPEG','WEBP'):
                        raise ValueError()
                    mime = {'PNG':'image/png','JPEG':'image/jpeg','WEBP':'image/webp'}[img.format]
                    img.verify()
        except Exception: raise ValueError('图片格式有误或尺寸过大，请使用 PNG、JPEG、WebP（最多 2500 万像素）。') from None
    elif suffix == '.pdf':
        if not raw.startswith(b'%PDF-'): raise ValueError('PDF 文件格式有误。')
        mime = 'application/pdf'
    elif suffix == '.docx':
        try:
            with zipfile.ZipFile(BytesIO(raw)) as archive:
                info = archive.getinfo('word/document.xml')
                if info.file_size > 2*1024*1024: raise ValueError()
                xml = ElementTree.fromstring(archive.read(info))
                text = '\n'.join(''.join(p.itertext()) for p in xml.iter('{http://schemas.openxmlformats.org/wordprocessingml/2006/main}p'))
        except Exception: raise ValueError('Word 文档格式有误或内容过大，请使用 DOCX 格式。') from None
    elif suffix in TEXT_TYPES:
        try: text = raw.decode('utf-8-sig')
        except UnicodeDecodeError:
            try: text = raw.decode('gb18030')
            except UnicodeDecodeError: raise ValueError('文本附件请使用 UTF-8 或 GB18030 编码。') from None
        if '\x00' in text: raise ValueError('文本附件含二进制数据，请检查文件类型。')
    else: raise ValueError('附件支持图片、PDF、TXT、Markdown、CSV、JSON、代码和 DOCX。')
    if text is not None and (not text.strip() or len(text) > 100000):
        raise ValueError('文档正文应为 1 至 100000 个字符，请拆分后发送。')
    return {'name': name, 'data': encoded, 'mime': mime, 'text': text, 'size':len(raw)}


def prepare_messages(messages, protocol):
    if not isinstance(messages, list) or not 1 <= len(messages) <= 80:
        raise ValueError('每次对话最多 80 条消息，请新建对话后继续。')
    result=[]; total_bytes=0; total_text=0
    for message in messages:
        if (not isinstance(message, dict) or set(message)-{'role','content','attachments'}
                or message.get('role') not in ('user','assistant')
                or not isinstance(message.get('content'), str) or len(message['content'])>32000):
            raise ValueError('消息内容格式有误，每条最多 32000 个字符。')
        files=message.get('attachments',[])
        if not isinstance(files,list) or len(files)>6 or (files and message['role']!='user'):
            raise ValueError('每条用户消息最多添加 6 个附件。')
        if not message['content'].strip() and not files: raise ValueError('请输入消息或添加附件。')
        text=message['content']; blocks=[]
        for value in files:
            item=attachment(value);total_bytes+=item['size']
            if total_bytes>MAX_TOTAL: raise ValueError('当前对话的附件合计最多 12 MB，请新建对话继续。')
            if item['text'] is not None:
                # Keep file contents within the user's message, never a privileged instruction.
                text+='\n\n[附件内容：'+item['name']+']\n'+item['text']+'\n[附件内容结束]'
            elif protocol=='anthropic':
                blocks.append({'type':'document' if item['mime']=='application/pdf' else 'image',
                               'source':{'type':'base64','media_type':item['mime'],'data':item['data']}})
            else:
                url='data:'+item['mime']+';base64,'+item['data']
                if item['mime']=='application/pdf':
                    block={'filename':item['name'],'file_data':url}
                    blocks.append(dict(type='input_file',**block) if protocol=='responses' else {'type':'file','file':block})
                else:
                    blocks.append({'type':'input_image','image_url':url} if protocol=='responses' else {'type':'image_url','image_url':{'url':url}})
        total_text+=len(text)
        if total_text>200000:raise ValueError('对话及文档正文合计最多 200000 字符，请新建对话。')
        if blocks:
            if text.strip():blocks.insert(0,{'type':'input_text' if protocol=='responses' else 'text','text':text})
            result.append({'role':message['role'],'content':blocks})
        else:result.append({'role':message['role'],'content':text})
    if messages[-1]['role']!='user':raise ValueError('请发送用户消息。')
    return result
