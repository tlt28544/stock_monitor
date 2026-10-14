"""Readable narrative PDF, companion Markdown/JSON, and one artifact validator."""
import hashlib
import json
import os
import tempfile
from datetime import datetime
from html import escape
from pathlib import Path
from xml.sax.saxutils import quoteattr

import reportlab
from pypdf import PdfReader
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer

from .models import DataQualityError, REPORT_ZONE

font_dir = Path(reportlab.__file__).parent / 'fonts'
pdfmetrics.registerFont(TTFont('Research', str(font_dir / 'Vera.ttf')))
pdfmetrics.registerFont(TTFont('ResearchBold', str(font_dir / 'VeraBd.ttf')))
pdfmetrics.registerFontFamily('Research', normal='Research', bold='ResearchBold',
                            italic='Research', boldItalic='ResearchBold')


def atomic_write(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix='.' + path.name, dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data.encode() if isinstance(data, str) else data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def validate_pdf(path, ticker, articles):
    try:
        reader = PdfReader(path)
        text = ' '.join((' '.join(p.extract_text() or '' for p in reader.pages)).split())
        if not reader.pages or ticker not in text or 'Monthly News Narrative' not in text or 'Sources' not in text:
            raise DataQualityError('PDF is missing required content')
        urls = {str(a.get_object().get('/A', {}).get('/URI', ''))
                for page in reader.pages for a in page.get('/Annots', [])}
        if not {a['url'] for a in articles} <= urls:
            raise DataQualityError('PDF is missing source links')
    except DataQualityError:
        raise
    except Exception as exc:
        raise DataQualityError('Unreadable PDF: ' + type(exc).__name__) from None


def validate_artifacts(artifacts, ticker, articles):
    if not artifacts:
        return False
    try:
        if set(artifacts) != {'pdf', 'md', 'json'}:
            return False
        if any(digest(a['path']) != a['sha256'] for a in artifacts.values()):
            return False
        validate_pdf(artifacts['pdf']['path'], ticker, articles)
        return True
    except (OSError, KeyError, ValueError, TypeError, DataQualityError):
        return False


def render_narrative(destination, ticker, company, report_date, version, start, end,
                     articles, research, notes, searches, demo=False):
    destination = Path(destination).resolve()
    destination.mkdir(parents=True, exist_ok=True)
    styles = getSampleStyleSheet()
    for name in styles.byName:
        styles[name].fontName = 'ResearchBold' if name in ('Title', 'Heading1', 'Heading2') else 'Research'
    styles.add(ParagraphStyle(name='Narrative', fontName='Research', fontSize=10, leading=14, spaceAfter=9))
    styles.add(ParagraphStyle(name='Source', fontName='Research', fontSize=8, leading=11, spaceAfter=5))
    title = f'{ticker} | {company} | Monthly News Narrative'
    story = [Paragraph(escape(title), styles['Title'])]
    md = [f'# {title}', '']
    metadata = f'Report date: {report_date} | Publication window: {start.date()} to {report_date} (Asia/Shanghai) | Version: {version}'
    story.append(Paragraph(escape(metadata), styles['Source']))
    md.append(metadata)
    if demo:
        notes = ['SYNTHETIC DEMO - NOT INVESTMENT RESEARCH'] + list(notes)
    for note in notes:
        story.append(Paragraph(escape(note), styles['Source']))
        md.append(note)
    story.append(Spacer(1, 12))
    index = {a['id']: i + 1 for i, a in enumerate(articles)}
    for block in research.narrative_blocks:
        story.append(Paragraph(escape(block.title), styles['Heading2']))
        md.extend(['', '## ' + block.title, ''])
        refs = ' '.join(f'[{index[ref]}]' for ref in dict.fromkeys(block.event_ids))
        for para in block.content.split('\n\n'):
            if para.strip():
                text = para.strip() + ' ' + refs
                story.append(Paragraph(escape(text), styles['Narrative']))
                md.extend([text, ''])
    if not articles:
        text = 'No usable dated news was found for this company in the requested month.'
        story.append(Paragraph(text, styles['Narrative']))
        md.extend(['', text])
    story.append(Paragraph('Sources', styles['Heading2']))
    md.extend(['', '## Sources', ''])
    for article in articles:
        publication = datetime.fromisoformat(article['published_at']).astimezone(REPORT_ZONE).date()
        text = f"[{index[article['id']]}] {publication} | {article['title']}"
        link = f'<link href={quoteattr(article["url"])} color="blue">{escape(text)}</link>'
        story.append(Paragraph(link, styles['Source']))
        md.append(f'- [{text}]({article["url"]})')
    if not articles:
        story.append(Paragraph('No sources passed the publication-date and content checks.', styles['Source']))
    fd, temporary = tempfile.mkstemp(suffix='.pdf', prefix=f'.{ticker}.', dir=destination)
    os.close(fd)
    try:
        def footer(canvas, document):
            canvas.saveState()
            canvas.setFont('Research', 7)
            canvas.drawString(48, 20, f'{ticker} | {report_date} | Page {document.page}')
            canvas.restoreState()
        doc = SimpleDocTemplate(temporary, pagesize=(612, 792), leftMargin=48, rightMargin=48,
                                topMargin=36, bottomMargin=36, title=title, author='Stock Monitor')
        doc.build(story, onFirstPage=footer, onLaterPages=footer)
        validate_pdf(temporary, ticker, articles)
        payload = {'ticker': ticker, 'company': company, 'report_date': report_date, 'version': version,
                   'demo': demo, 'window_start': start.isoformat(), 'window_end_exclusive': end.isoformat(),
                   'articles': articles, 'research': research.model_dump(), 'notes': notes, 'searches': searches}
        atomic_write(destination / f'{ticker}.md', '\n'.join(md) + '\n')
        atomic_write(destination / f'{ticker}.json', json.dumps(payload, ensure_ascii=False, indent=2) + '\n')
        os.replace(temporary, destination / f'{ticker}.pdf')
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return {ext: {'path': str(destination / f'{ticker}.{ext}'), 'sha256': digest(destination / f'{ticker}.{ext}')}
            for ext in ('pdf', 'md', 'json')}
