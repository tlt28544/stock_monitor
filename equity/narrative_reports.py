"""Narrative-style institutional report renderer."""
import hashlib
import json
import os
import tempfile
from decimal import Decimal
from html import escape
from pathlib import Path
from xml.sax.saxutils import quoteattr
from pypdf import PdfReader
import reportlab
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.lib import colors
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, PageBreak, KeepInFrame
from .models import DataQualityError
from .valuation import display


_font_dir = Path(reportlab.__file__).parent / 'fonts'
pdfmetrics.registerFont(TTFont('Research', str(_font_dir / 'Vera.ttf')))
pdfmetrics.registerFont(TTFont('ResearchBold', str(_font_dir / 'VeraBd.ttf')))
pdfmetrics.registerFontFamily('Research', normal='Research', bold='ResearchBold', italic='Research', boldItalic='ResearchBold')


def atomic_write(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix='.' + path.name + '.', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as f:
            f.write(data.encode() if isinstance(data, str) else data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def valuation_rows(v):
    rows = [['Business / Adjustment', 'Fundamental Driver', 'Valuation Driver', 'Implied Segment Value']]
    for row in v['rows']:
        rows.append([row['name'], row['fundamental'], row['driver'], display(Decimal(row['value'])) + 'm' if row['value'] is not None else 'N/A'])
    rows.extend([
        ['Total Equity Value', '', v['currency'] + ' millions', display(Decimal(v['equity_value'])) if v['equity_value'] is not None else 'N/A'],
        ['Implied Price per Share', '', '', display(Decimal(v['implied_price'])) if v['implied_price'] is not None else 'N/A'],
        ['Upside/Downside', '', '', display(Decimal(v['upside'])) + '%' if v['upside'] is not None else 'N/A'],
        ['Valuation As-of Date', '', '', v['as_of']]])
    return rows


def render_narrative(destination, ticker, date, version, events, research, valuation, warnings, demo=False, market_context=None):
    """Render institutional narrative-style report with deep thematic sections."""
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    
    styles = getSampleStyleSheet()
    for name in styles.byName:
        styles[name].fontName = 'ResearchBold' if name in ('Title', 'Heading1', 'Heading2') else 'Research'
    
    # Add narrative-specific styles
    styles.add(ParagraphStyle(name='Narrative', fontName='Research', fontSize=9, leading=11.5, spaceAfter=6, alignment=4))  # Justified
    styles.add(ParagraphStyle(name='SectionTitle', fontName='ResearchBold', fontSize=11, leading=13, spaceBefore=10, spaceAfter=6))
    styles.add(ParagraphStyle(name='Compact', fontName='Research', fontSize=8.2, leading=10.5, spaceAfter=4))
    styles.add(ParagraphStyle(name='Tiny', fontName='Research', fontSize=6.8, leading=8.1, spaceAfter=2))
    styles.add(ParagraphStyle(name='HeaderContext', fontName='Research', fontSize=8.5, leading=10, spaceAfter=6, textColor=colors.HexColor('#444444')))
    
    story, md = [], []
    index = {e['id']: i + 1 for i, e in enumerate(events)}
    
    def p(text, style='Compact', raw=False):
        return Paragraph(text if raw else escape(text), styles[style])
    
    def heading(text, style='Heading2'):
        story.append(p(text, style))
        md.append('\n## ' + text + '\n')
    
    # Title and context
    label = 'SYNTHETIC DEMO - NOT INVESTMENT RESEARCH' if demo else 'Institutional Intelligence & Valuation Dossier'
    title = f'{ticker} — {label}'
    story.append(p(title, 'Title'))
    md.append(f'# {title}\n')
    
    # Market context header (if available)
    if market_context:
        context_line = f"Market Price: {market_context.get('price', 'N/A')} | Market Cap: {market_context.get('market_cap', 'N/A')} | P/E: {market_context.get('pe', 'N/A')}"
        story.append(p(context_line, 'HeaderContext'))
        md.append(context_line + '\n')
    
    meta = f'Report Date: {date} | Version {version} | Trailing 90-day window'
    story.append(p(meta, 'Tiny'))
    md.append(meta + '\n')
    
    # Coverage notes
    coverage = '; '.join(warnings) if warnings else 'Provider coverage limitations apply'
    story.append(p(coverage, 'Tiny'))
    md.append(coverage + '\n')
    
    story.append(Spacer(1, 12))
    
    # Narrative blocks (main content)
    for block in research.narrative_blocks:
        heading(block.title, 'SectionTitle')
        
        # Parse narrative content into paragraphs
        paragraphs = block.content.strip().split('\n\n')
        for para in paragraphs:
            if para.strip():
                # Add event references at the end if not already present
                refs = ' '.join(f'[{index[r]}]' for r in block.event_ids)
                if not any(f'[{index[r]}]' in para for r in block.event_ids):
                    para_with_refs = para.strip() + ' ' + refs
                else:
                    para_with_refs = para.strip()
                
                story.append(p(para_with_refs, 'Narrative'))
                md.append(para + '\n')
        
        # Key metrics box (if any)
        if block.key_metrics:
            metrics_text = ' • '.join(block.key_metrics)
            story.append(p(f'Key Metrics: {metrics_text}', 'Tiny'))
            md.append(f'\n**Key Metrics:** {metrics_text}\n')
        
        story.append(Spacer(1, 8))
    
    # Page break before sources and valuation
    story.append(PageBreak())
    
    # Sources section
    heading('Sources & Event Index')
    for e in events:
        for s in e['sources'][:2]:
            text = f"[{index[e['id']]}] {e['date']} {s['provider']}: {e['title'][:85]}"
            link = f'<link href={quoteattr(s["url"])} color="blue">{escape(text)}</link>'
            story.append(p(link, 'Tiny', raw=True))
            md.append(f'- [{text}]({s["url"]})\n')
    
    story.append(Spacer(1, 12))
    
    # Valuation review
    heading('Valuation Assumptions to Review')
    if research.valuation_review:
        for item in research.valuation_review:
            story.append(p('• ' + item, 'Compact'))
            md.append(f'- {item}\n')
    else:
        story.append(p('No specific valuation assumptions flagged for review.', 'Compact'))
        md.append('No specific valuation assumptions flagged for review.\n')
    
    # Valuation limitations
    if valuation.get('warnings'):
        text = 'Valuation limitations: ' + '; '.join(valuation['warnings'])
        story.append(p(text[:1500], 'Tiny'))
        md.append('\n' + text + '\n')
    
    # Provenance
    provenance = (f"Analyst reference revision: {valuation.get('revision') or 'N/A'} | Input as-of: {valuation.get('input_as_of') or 'N/A'} | "
                  f"Current price: {valuation.get('current_share_price') or 'N/A'} | Price timestamp: {valuation.get('price_timestamp') or 'N/A'}")
    story.append(p(provenance, 'Tiny'))
    md.append(provenance + '\n')
    
    # Valuation table
    heading('Valuation Table')
    rows = valuation_rows(valuation)
    cells = [[p(cell, 'Tiny') for cell in row] for row in rows]
    table = Table(cells, colWidths=[120, 135, 135, 114], repeatRows=1)
    table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#e4eaf2')),
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('GRID', (0, 0), (-1, -1), .35, colors.HexColor('#bdc5cf')),
        ('LEFTPADDING', (0, 0), (-1, -1), 4),
        ('RIGHTPADDING', (0, 0), (-1, -1), 4),
        ('TOPPADDING', (0, 0), (-1, -1), 4),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 4)]))
    story.append(table)
    
    md.append('\n| ' + ' | '.join(rows[0]) + ' |\n|' + '|'.join(['---'] * 4) + '|\n')
    md.extend('| ' + ' | '.join(row) + ' |\n' for row in rows[1:])
    
    # Generate PDF
    fd, temporary = tempfile.mkstemp(suffix='.pdf', prefix=f'.{ticker}.', dir=destination)
    os.close(fd)
    
    try:
        doc = SimpleDocTemplate(temporary, pagesize=(612, 792), rightMargin=54, leftMargin=54, topMargin=35, bottomMargin=35,
                                title=title, author='Equity Research Pipeline')
        
        def footer(canvas, document):
            canvas.saveState()
            canvas.setFont('Research', 7)
            canvas.drawString(54, 20, f'{ticker} | {date} | {version} | Page {document.page}')
            canvas.restoreState()
        
        doc.build(story, onFirstPage=footer, onLaterPages=footer)
        
        # Verify PDF page count
        reader = PdfReader(temporary)
        if len(reader.pages) > 4:
            raise DataQualityError(f'Report exceeds 4 pages ({len(reader.pages)} pages); content too verbose')
        
        # Write final outputs
        pdf_path = destination / f'{ticker}.pdf'
        md_path = destination / f'{ticker}.md'
        json_path = destination / f'{ticker}.json'
        
        atomic_write(pdf_path, Path(temporary).read_bytes())
        atomic_write(md_path, '\n'.join(md))
        
        # JSON metadata
        metadata = {
            'ticker': ticker,
            'report_date': date,
            'version': version,
            'events': events,
            'research': research.model_dump() if hasattr(research, 'model_dump') else research,
            'valuation': valuation,
            'warnings': warnings,
            'market_context': market_context
        }
        atomic_write(json_path, json.dumps(metadata, indent=2, ensure_ascii=False))
        
        return {
            'pdf': {'path': str(pdf_path), 'sha256': digest(pdf_path)},
            'md': {'path': str(md_path), 'sha256': digest(md_path)},
            'json': {'path': str(json_path), 'sha256': digest(json_path)}
        }
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
