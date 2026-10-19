"""Local attachment storage and bounded document extraction."""
import base64
import io
import json
import re
import uuid
import zipfile
from pathlib import Path
from xml.etree import ElementTree

TEXT_EXTENSIONS = {'.txt','.md','.csv','.json','.py','.js','.jsx','.ts','.tsx','.html','.css','.xml','.yaml','.yml','.log','.ini','.toml','.sql','.c','.cpp','.h','.java','.rs','.go','.sh','.bat','.ps1'}


class Attachments:
    def __init__(self, directory):
        self.root = Path(directory)/"attachments"
        self.root.mkdir(parents=True, exist_ok=True)

    def upload(self, name, encoded):
        name = str(name).replace('\\','/').split('/')[-1][:180]
        if not name:
            raise ValueError("附件名称为空")
        try:
            raw = base64.b64decode(encoded, validate=True)
        except (ValueError, TypeError):
            raise ValueError("附件数据格式有误") from None
        if not raw or len(raw) > 10_000_000:
            raise ValueError("单个附件限 10 MB")
        extension = Path(name).suffix.lower()
        text, kind = '', 'text'
        if extension in ('.png','.jpg','.jpeg','.webp'):
            valid = raw.startswith((b'\x89PNG\r\n\x1a\n',b'\xff\xd8\xff')) or (raw[:4] == b'RIFF' and raw[8:12] == b'WEBP')
            if not valid:
                raise ValueError("图片内容与格式不匹配")
            kind = 'image'
        elif extension == '.pdf':
            from pypdf import PdfReader
            reader = PdfReader(io.BytesIO(raw))
            if len(reader.pages) > 200:
                raise ValueError("PDF 限 200 页")
            parts, total = [], 0
            for page in reader.pages:
                part = page.extract_text() or ''
                parts.append(part)
                total += len(part)
                if total > 120000:
                    break
            text = '\n'.join(parts)
            if not text.strip():
                raise ValueError("PDF 未提取到文字。扫描件可上传页面图片")
        elif extension == '.docx':
            with zipfile.ZipFile(io.BytesIO(raw)) as document:
                item = document.getinfo('word/document.xml')
                if item.file_size > 20_000_000:
                    raise ValueError("Word 文档解压内容过大")
                xml = document.read(item)
                if b'<!ENTITY' in xml or b'<!DOCTYPE' in xml:
                    raise ValueError("Word 文档包含不支持的 XML 结构")
                tree = ElementTree.fromstring(xml)
                ns = {'w':'http://schemas.openxmlformats.org/wordprocessingml/2006/main'}
                text = '\n'.join(''.join(p.itertext()) for p in tree.findall('.//w:p',ns))
        elif extension in TEXT_EXTENSIONS:
            for encoding in ('utf-8-sig','gb18030'):
                try:
                    text = raw.decode(encoding)
                    break
                except UnicodeError:
                    pass
            if not text or '\0' in text:
                raise ValueError("请使用 UTF-8 或 GB18030 文本附件")
        else:
            raise ValueError("支持文本、代码、PDF、DOCX，以及 PNG / JPG / WebP 图片")
        identity = uuid.uuid4().hex
        metadata = {'id':identity,'name':name,'size':len(raw),'kind':kind,'truncated':len(text)>120000}
        (self.root/(identity+'.bin')).write_bytes(raw)
        (self.root/(identity+'.json')).write_text(json.dumps({**metadata,'text':text[:120000]},ensure_ascii=False),encoding='utf-8')
        return metadata

    def load(self, identity):
        if not isinstance(identity,str) or not re.fullmatch('[0-9a-f]{32}',identity):
            raise ValueError("附件编号有误")
        try:
            metadata = json.loads((self.root/(identity+'.json')).read_text(encoding='utf-8'))
            image = base64.b64encode((self.root/(identity+'.bin')).read_bytes()).decode() if metadata['kind']=='image' else None
            return metadata, image
        except OSError:
            raise ValueError("附件已不存在，请重新上传") from None
