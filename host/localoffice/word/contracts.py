"""Closed, bounded model-facing Word contracts; no file access or commit authority.

IDs and hashes are supplied by the host/worker, not inferred by the model.
Runtime must separately enforce project scope, ownership and revision validity.
"""
from typing import Annotated, Literal, Any
from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator, model_validator, model_serializer

Hash = Annotated[str, Field(pattern=r'^[0-9a-f]{64}$')]
ObjectId = Annotated[str, Field(min_length=1, max_length=160)]
ObjectHash = Annotated[str, Field(pattern=r'^(?:[0-9a-f]{64}|ev_[0-9a-f]{16})$')]
MAX_BATCH_BYTES = 32_768
MAX_OPERATIONS = 24
MAX_RESULT_BYTES = 16_384


class Contract(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)


class LoadTools(Contract):
    tool: Literal['word.load_tools'] = 'word.load_tools'
    operations: list[Literal['plan_requirements','plan_draft','plan_content','repair_content_plan','select_source_quotes','inspect','replace_text','format_text','rename_style','create_paragraph_style','create_character_style','set_character_style','set_style','format_style_typography','format_style','format_paragraph','insert_paragraphs','delete_paragraph','insert_table','merge_cells','unmerge_cells','set_cell_text','insert_rows','format_cell','format_row','format_table','set_section_layout','set_list','insert_image','replace_image','format_image','set_image_layout','import_image','create','repair_creation','prepare_creation','finalize_creation','revise_creation','edit_creation_blocks','edit_creation_styles','refresh_fields']] = Field(min_length=1,max_length=4)

    @field_validator('operations')
    @classmethod
    def unique_operations(cls,value):
        if len(set(value))!=len(value):raise ValueError('Load each operation once.')
        return value


class Target(Contract):
    path: str = Field(min_length=1, max_length=1024)
    revision: Hash

    @field_validator('path')
    @classmethod
    def relative_docx(cls, value):
        if (value.startswith('/') or '\\' in value or '\x00' in value
                or any(part in ('', '.', '..', '.localoffice') for part in value.split('/'))
                or not value.lower().endswith('.docx')):
            raise ValueError('Use a project-relative DOCX path outside managed state.')
        return value


class RefreshFields(Target):
    tool: Literal['word.refresh_fields'] = 'word.refresh_fields'


class ListObjects(Target):
    tool: Literal['word.list_objects'] = 'word.list_objects'
    parent: ObjectId | None = None
    kind: Literal['paragraph', 'table', 'row', 'cell', 'image', 'section', 'style', 'numbering', 'header', 'footer'] | None = None
    cursor: str | None = Field(default=None, max_length=256)
    limit: int = Field(default=20, ge=1, le=50)
    # Index entries contain identifiers and previews, never unbounded full content.
    preview_chars: int = Field(default=80, ge=0, le=160)


class FindText(Target):
    tool: Literal['word.find_text'] = 'word.find_text'
    query: str = Field(min_length=1,max_length=200)
    scope: Literal['main','headers','footers','all'] = 'main'
    case_sensitive: bool = True
    limit: int = Field(default=5,ge=1,le=8)
    cursor: str | None = Field(default=None,max_length=256)

    @field_validator('query')
    @classmethod
    def valid_query(cls,value):
        if any((ord(c)<32 and c not in '\t\n\r') or 0xD800<=ord(c)<=0xDFFF for c in value):
            raise ValueError('Search requires valid Unicode text without XML control characters.')
        return value


class ReadObjects(Target):
    tool: Literal['word.read_objects'] = 'word.read_objects'
    ids: list[ObjectId] = Field(min_length=1, max_length=8)
    offset: int = Field(default=0, ge=0)
    max_chars: int = Field(default=4000, ge=1, le=8000)
    include_format: bool = False

    @field_validator('ids')
    @classmethod
    def unique_ids(cls, value):
        if len(set(value)) != len(value):
            raise ValueError('Read each object once per call.')
        return value


class Edit(Contract):
    id: ObjectId | None = None
    target: ObjectId
    expected_hash: ObjectHash


class ReplacementSpan(Contract):
    start: int = Field(ge=0)
    end: int = Field(ge=0)
    text: str = Field(max_length=8000)

    @model_validator(mode='after')
    def valid_span(self):
        if self.end < self.start or '\x00' in self.text:
            raise ValueError('Invalid text span or NUL in replacement.')
        return self


class ReplaceText(Edit,ReplacementSpan):
    """Replace or clear text inside a paragraph; the paragraph itself remains. Use delete_paragraph to remove the paragraph."""
    op: Literal['replace_text'] = 'replace_text'


class ReplaceTextCall(Target,ReplacementSpan):
    """Replace or clear text inside a paragraph; even empty text retains the paragraph. For paragraph removal load delete_paragraph and use word.stage_batch."""
    tool: Literal['word.replace_text','replace_text'] = 'word.replace_text'
    target: ObjectId
    expected_hash: ObjectHash

    def as_batch(self):
        return StageBatch(path=self.path,revision=self.revision,operations=[{
            'op':'replace_text',**self.model_dump(exclude={'tool','path','revision'})}])


class SetStyle(Edit):
    op: Literal['set_style'] = 'set_style'
    style_id: ObjectId


class Typography(Contract):
    font: str | None = Field(default=None,min_length=1,max_length=80)
    size_half_points: int | None = Field(default=None,ge=12,le=288)
    size_points: float | None = Field(default=None,ge=6,le=144,multiple_of=0.5)
    color: str | None = Field(default=None,pattern=r'^[0-9A-Fa-f]{6}$')
    bold: bool | None = None
    italic: bool | None = None

    @model_validator(mode='after')
    def has_change(self):
        if not self.model_dump(exclude_none=True):raise ValueError('Specify at least one formatting property.')
        if self.size_points is not None and self.size_half_points is not None:
            raise ValueError('Supply font size in points or half-points, not both.')
        return self


class TextSpan(Contract):
    start: int = Field(ge=0)
    end: int = Field(ge=1)
    properties: Typography

    @model_validator(mode='after')
    def nonempty_span(self):
        if self.end<=self.start:raise ValueError('Formatting needs a nonempty text span.')
        return self


class FormatText(Edit,TextSpan):
    op: Literal['format_text'] = 'format_text'


class FormatTextCall(Target,TextSpan):
    tool: Literal['word.format_text','format_text'] = 'word.format_text'
    target: ObjectId
    expected_hash: ObjectHash

    def as_batch(self):
        return StageBatch(path=self.path,revision=self.revision,operations=[{
            'op':'format_text',**self.model_dump(exclude={'tool','path','revision'})}])


class ParagraphFormat(Typography):
    alignment: Literal['left','center','right','both'] | None = None
    spacing_before_twips: int | None = Field(default=None,ge=0,le=2880)
    spacing_after_twips: int | None = Field(default=None,ge=0,le=2880)
    left_indent_twips: int | None = Field(default=None,ge=0,le=14400)
    right_indent_twips: int | None = Field(default=None,ge=0,le=14400)
    first_line_twips: int | None = Field(default=None,ge=-7200,le=7200)
    keep_with_next: bool | None = None
    keep_lines: bool | None = None
    page_break_before: bool | None = None

class FormatParagraph(Edit):
    op: Literal['format_paragraph'] = 'format_paragraph'
    properties: ParagraphFormat


class FormatStyleTypography(Edit):
    op: Literal['format_style_typography'] = 'format_style_typography'
    properties: Typography


class FormatStyle(Edit):
    op: Literal['format_style'] = 'format_style'
    properties: ParagraphFormat


class CreateParagraphStyle(Edit):
    op: Literal['create_paragraph_style'] = 'create_paragraph_style'
    style_id: str = Field(pattern=r'^[A-Za-z][A-Za-z0-9_-]{0,63}$')
    name: str = Field(min_length=1,max_length=120)
    properties: ParagraphFormat | None = None

    @field_validator('name')
    @classmethod
    def valid_name(cls,value):
        if not value.strip() or any(ord(c)<32 for c in value):
            raise ValueError('Style name must be nonblank and contain no control characters.')
        return value


class CreateCharacterStyle(CreateParagraphStyle):
    op: Literal['create_character_style'] = 'create_character_style'
    inherit_base: bool = True
    properties: Typography | None = None


class SetCharacterStyle(Edit):
    op: Literal['set_character_style'] = 'set_character_style'
    style_id: ObjectId
    start: int = Field(ge=0)
    end: int = Field(ge=1)

    @model_validator(mode='after')
    def nonempty(self):
        if self.end <= self.start:raise ValueError('Select a nonempty text span.')
        return self


class RenameStyle(Edit):
    op: Literal['rename_style'] = 'rename_style'
    name: str = Field(min_length=1,max_length=120)

    @field_validator('name')
    @classmethod
    def valid_name(cls,value):
        if not value.strip() or any(ord(c)<32 or 0xD800<=ord(c)<=0xDFFF for c in value):
            raise ValueError('Style name must be nonblank valid Unicode without control characters.')
        return value


class InsertedParagraph(Contract):
    text: str = Field(max_length=8000)
    style: str = Field(default='Normal',pattern=r'^[A-Za-z][A-Za-z0-9_-]{0,63}$')
    page_break_before: bool = False

    @field_validator('text')
    @classmethod
    def valid_xml_text(cls,value):
        if any(ord(c)<32 and c not in '\t\n\r' for c in value):
            raise ValueError('Paragraph text contains an invalid XML control character.')
        return value


class InsertParagraphs(Edit):
    op: Literal['insert_paragraphs'] = 'insert_paragraphs'
    position: Literal['before','after'] = 'after'
    paragraphs: list[InsertedParagraph] = Field(min_length=1,max_length=24)


class DeleteParagraph(Edit):
    """Remove the paragraph object itself. Use replace_text to change or clear its text while retaining the paragraph."""
    op: Literal['delete_paragraph'] = 'delete_paragraph'


class InsertImage(Edit):
    op: Literal['insert_image'] = 'insert_image'
    asset_id: str = Field(pattern=r'^image:[0-9a-f]{64}$')
    width_inches: float = Field(ge=0.1,le=22)
    position: Literal['before','after'] = 'after'
    alignment: Literal['left','center','right'] = 'center'
    alt_text: str = Field(min_length=1,max_length=1000)
    caption: str | None = Field(default=None,min_length=1,max_length=2000)


class ReplaceImage(Edit):
    op: Literal['replace_image'] = 'replace_image'
    asset_id: str = Field(pattern=r'^image:[0-9a-f]{64}$')


class ImageFormat(Contract):
    width_inches: float | None = Field(default=None,ge=0.1,le=22)
    height_inches: float | None = Field(default=None,ge=0.1,le=22)
    crop_left_percent: float | None = Field(default=None,ge=0,lt=100,multiple_of=0.001)
    crop_top_percent: float | None = Field(default=None,ge=0,lt=100,multiple_of=0.001)
    crop_right_percent: float | None = Field(default=None,ge=0,lt=100,multiple_of=0.001)
    crop_bottom_percent: float | None = Field(default=None,ge=0,lt=100,multiple_of=0.001)
    alt_text: str | None = Field(default=None,min_length=1,max_length=1000)

    @model_validator(mode='after')
    def valid_changes(self):
        if not self.model_dump(exclude_none=True):raise ValueError('Specify at least one image property.')
        for first,last in [(self.crop_left_percent,self.crop_right_percent),(self.crop_top_percent,self.crop_bottom_percent)]:
            if first is not None and last is not None and first+last>=100:
                raise ValueError('Opposing crops must leave a positive visible area.')
        return self


class FormatImage(Edit):
    op: Literal['format_image'] = 'format_image'
    properties: ImageFormat


class ImageLayoutProperties(Contract):
    mode: Literal['inline','floating'] | None = None
    wrap: Literal['square','top_bottom','none'] | None = None
    horizontal_origin: Literal['page','margin','column'] | None = None
    vertical_origin: Literal['page','margin','paragraph'] | None = None
    horizontal_offset_inches: float | None = Field(default=None,ge=-22,le=22)
    vertical_offset_inches: float | None = Field(default=None,ge=-22,le=22)

    @model_validator(mode='after')
    def valid_changes(self):
        fields=self.model_dump(exclude_none=True)
        if not fields:raise ValueError('Specify at least one image layout property.')
        if self.mode=='inline' and len(fields)!=1:
            raise ValueError('Inline conversion accepts only mode.')
        return self


class SetImageLayout(Edit):
    op: Literal['set_image_layout'] = 'set_image_layout'
    properties: ImageLayoutProperties


CellText = Annotated[str,Field(max_length=8000)]


class SetCellText(Edit):
    op: Literal['set_cell_text'] = 'set_cell_text'
    text: CellText

    @field_validator('text')
    @classmethod
    def valid_text(cls,value):
        return InsertedParagraph.valid_xml_text(value)


class InsertRows(Edit):
    op: Literal['insert_rows'] = 'insert_rows'
    position: Literal['before','after'] = 'after'
    rows: list[list[CellText]] = Field(min_length=1,max_length=8)
    header_row: bool = False

    @field_validator('rows')
    @classmethod
    def bounded_rows(cls,rows):
        width=len(rows[0])
        if not 1<=width<=12 or any(len(row)!=width for row in rows):
            raise ValueError('Inserted rows require 1–12 cells with consistent width.')
        for row in rows:
            for text in row:InsertedParagraph.valid_xml_text(text)
        return rows


class SectionLayout(Contract):
    width_twips: int | None = Field(default=None,ge=1440,le=31680)
    height_twips: int | None = Field(default=None,ge=1440,le=31680)
    top_twips: int | None = Field(default=None,ge=0,le=14400)
    bottom_twips: int | None = Field(default=None,ge=0,le=14400)
    left_twips: int | None = Field(default=None,ge=0,le=14400)
    right_twips: int | None = Field(default=None,ge=0,le=14400)
    header_twips: int | None = Field(default=None,ge=0,le=14400)
    footer_twips: int | None = Field(default=None,ge=0,le=14400)
    gutter_twips: int | None = Field(default=None,ge=0,le=14400)

    @model_validator(mode='after')
    def explicit_dimensions(self):
        if not self.model_dump(exclude_none=True):raise ValueError('Specify at least one section layout property.')
        if (self.width_twips is None)!=(self.height_twips is None):
            raise ValueError('Specify both page dimensions; orientation is derived from them.')
        return self


class SetSectionLayout(Edit):
    op: Literal['set_section_layout'] = 'set_section_layout'
    properties: SectionLayout


class TableBorder(Contract):
    style: Literal['single','double','dashed','dotted','nil'] = 'single'
    color: str = Field(default='000000',pattern=r'^[0-9A-Fa-f]{6}$')
    size_eighth_points: int = Field(default=4,ge=2,le=96)


class ColumnWidths(Contract):
    unit: Literal['in','cm','pt','twip']
    values: list[Annotated[float,Field(gt=0,le=31680,allow_inf_nan=False)]] = Field(min_length=1,max_length=12)

    def twips(self):
        from decimal import Decimal,ROUND_HALF_UP
        factor={'in':Decimal(1440),'cm':Decimal(144000)/Decimal(254),'pt':Decimal(20),'twip':Decimal(1)}[self.unit]
        return [int((Decimal(str(v))*factor).quantize(Decimal(1),rounding=ROUND_HALF_UP)) for v in self.values]


class TableFormat(Contract):
    column_widths: ColumnWidths | None = None
    column_widths_twips: list[Annotated[int,Field(ge=144,le=31680)]] | None = Field(default=None,min_length=1,max_length=12)
    alignment: Literal['left','center','right'] | None = None
    borders: TableBorder | None = None
    padding_top_twips: int | None = Field(default=None,ge=0,le=2880)
    padding_bottom_twips: int | None = Field(default=None,ge=0,le=2880)
    padding_left_twips: int | None = Field(default=None,ge=0,le=2880)
    padding_right_twips: int | None = Field(default=None,ge=0,le=2880)

    @model_validator(mode='after')
    def bounded_layout(self):
        if not self.model_dump(exclude_none=True):raise ValueError('Specify at least one table property.')
        if self.column_widths and self.column_widths_twips:
            raise ValueError('Supply one column-width representation, not both.')
        widths=self.column_widths.twips() if self.column_widths else self.column_widths_twips
        if widths and (min(widths)<144 or sum(widths)>31680):
            raise ValueError('Columns must be at least 144 twips and total width at most 31680 twips.')
        return self


class FormatTable(Edit):
    op: Literal['format_table'] = 'format_table'
    properties: TableFormat


class CellFormat(Contract):
    fill: str | None = Field(default=None,pattern=r'^(?:[0-9A-Fa-f]{6}|auto)$')
    vertical_alignment: Literal['top','center','bottom'] | None = None
    padding_top_twips: int | None = Field(default=None,ge=0,le=2880)
    padding_bottom_twips: int | None = Field(default=None,ge=0,le=2880)
    padding_left_twips: int | None = Field(default=None,ge=0,le=2880)
    padding_right_twips: int | None = Field(default=None,ge=0,le=2880)

    @model_validator(mode='after')
    def nonempty(self):
        if not self.model_dump(exclude_none=True):raise ValueError('Specify at least one cell property.')
        return self


class FormatCell(Edit):
    op: Literal['format_cell'] = 'format_cell'
    properties: CellFormat


class MergeCells(Edit):
    op: Literal['merge_cells'] = 'merge_cells'
    row_start: int = Field(ge=0,le=100000)
    row_count: int = Field(ge=1,le=20)
    column_start: int = Field(ge=0,le=100000)
    column_count: int = Field(ge=1,le=12)

    @model_validator(mode='after')
    def multiple_cells(self):
        if self.row_count*self.column_count<2:raise ValueError('Merge at least two cells.')
        return self


class UnmergeCells(Edit):
    op: Literal['unmerge_cells'] = 'unmerge_cells'


class RowHeight(Contract):
    twips: int = Field(ge=20,le=31680)
    rule: Literal['at_least','exact'] = 'at_least'


class RowFormat(Contract):
    height: RowHeight | None = None
    repeat_header: bool | None = None
    allow_split: bool | None = None

    @model_validator(mode='after')
    def nonempty(self):
        if not self.model_dump(exclude_none=True):raise ValueError('Specify at least one row property.')
        return self


class FormatRow(Edit):
    op: Literal['format_row'] = 'format_row'
    properties: RowFormat


class InsertTable(Edit):
    op: Literal['insert_table'] = 'insert_table'
    position: Literal['before','after'] = 'after'
    rows: list[list[Annotated[str,Field(max_length=2000)]]] = Field(min_length=1,max_length=20)
    column_widths_twips: list[Annotated[int,Field(ge=144,le=31680)]] = Field(min_length=1,max_length=12)
    header_row: bool = True
    style: str = Field(default='Normal',pattern=r'^[A-Za-z][A-Za-z0-9_-]{0,63}$')

    @model_validator(mode='after')
    def rectangular(self):
        if sum(self.column_widths_twips)>31680:
            raise ValueError('Table width must not exceed 31680 twips.')
        if any(len(row)!=len(self.column_widths_twips) for row in self.rows):
            raise ValueError('Provide one explicit width per table column and rectangular rows.')
        for row in self.rows:
            for text in row:InsertedParagraph.valid_xml_text(text)
        return self


class SetList(Edit):
    op: Literal['set_list'] = 'set_list'
    number_id: int = Field(ge=0,le=2147483647)
    level: int = Field(default=0,ge=0,le=8)

    restart_at: int | None = Field(default=None,ge=1,le=32767)

    @model_serializer(mode='wrap')
    def legacy_list_shape(self, handler):
        result=handler(self)
        if self.restart_at is None:result.pop('restart_at',None)
        return result

    @model_validator(mode='after')
    def removal_level(self):
        if self.number_id==0 and self.level!=0:raise ValueError('List removal uses number_id=0 and level=0.')
        if self.restart_at is not None and (self.number_id==0 or self.level!=0):
            raise ValueError('Restart requires a positive list ID and level 0.')
        return self


Patch = Annotated[ReplaceText | FormatText | RenameStyle | CreateParagraphStyle | CreateCharacterStyle | SetCharacterStyle | ReplaceImage | FormatImage | SetImageLayout | InsertImage | SetStyle | FormatStyle | FormatStyleTypography | InsertParagraphs | DeleteParagraph | FormatParagraph | SetCellText | InsertRows | InsertTable | SetSectionLayout | FormatTable | FormatCell | FormatRow | MergeCells | UnmergeCells | SetList, Field(discriminator='op')]


class StageBatch(Target):
    tool: Literal['word.stage_batch'] = 'word.stage_batch'
    operations: list[Patch] = Field(min_length=1, max_length=MAX_OPERATIONS)

    @model_validator(mode='after')
    def bounded_unique_batch(self):
        # IDs label results within a batch; targets/hashes carry mutation authority.
        # Assign bookkeeping IDs deterministically, without changing caller-owned edits.
        explicit=[op.id for op in self.operations if op.id is not None]
        if len(explicit)!=len(set(explicit)):
            raise ValueError('Operation IDs must be unique within a batch.')
        used=set(explicit);number=1;normalized=[]
        for op in self.operations:
            if op.id is None:
                while f'operation-{number}' in used:number+=1
                assigned=f'operation-{number}';used.add(assigned);number+=1
                op=op.model_copy(update={'id':assigned})
            normalized.append(op)
        self.operations=normalized
        if any(isinstance(op,(InsertParagraphs,DeleteParagraph,InsertRows,InsertTable,MergeCells,UnmergeCells,InsertImage,ReplaceImage,CreateParagraphStyle,CreateCharacterStyle)) for op in self.operations) and len(self.operations)!=1:
            raise ValueError('A structural change, style creation or image replacement is its own bounded batch; refresh object IDs before further edits.')
        if len({op.id for op in self.operations}) != len(self.operations):
            raise ValueError('Operation IDs must be unique within a batch.')
        # Initial contract allows one operation per target; all preconditions
        # resolve against the same input revision. Later chaining needs explicit semantics.
        if len({op.target for op in self.operations}) != len(self.operations):
            raise ValueError('Use separate batches for multiple edits to one target.')
        if len(self.model_dump_json().encode('utf-8')) > MAX_BATCH_BYTES:
            raise ValueError('Batch exceeds 32 KiB UTF-8 wire budget.')
        return self


class EditResult(Contract):
    id: ObjectId
    target: ObjectId
    before_hash: Hash
    after_hash: Hash | None
    deleted: bool = False
    summary: str = Field(max_length=240)
    inserted_ids: list[ObjectId] = Field(default_factory=list,max_length=24)
    number_id: int | None = Field(default=None,ge=1,le=2147483647)


    @model_validator(mode='after')
    def deletion_receipt(self):
        if self.deleted != (self.after_hash is None):
            raise ValueError('Deleted objects have no after hash; retained objects require one.')
        if self.deleted and self.inserted_ids:
            raise ValueError('Deletion cannot report inserted objects.')
        return self


class BatchReceipt(Contract):
    version: Literal[1] = 1
    status: Literal['validated', 'unchanged'] = 'validated'
    receipt_id: ObjectId | None = None
    input_revision: Hash | None
    candidate_revision: Hash
    results: list[EditResult] = Field(default_factory=list, max_length=MAX_OPERATIONS)
    warnings: list[Annotated[str, Field(max_length=240)]] = Field(default_factory=list, max_length=8)
    detail_handle: ObjectId | None = None
    creation_verification: dict[str, JsonValue] | None = None

    @field_validator('creation_verification')
    @classmethod
    def bounded_creation_verification(cls,value):
        if value is None:return value
        import json
        allowed={'scope','omitted','counts','body','bands','styles','section_layout'}
        if set(value)-allowed or not isinstance(value.get('scope'),str) or value.get('omitted') is not True:
            raise ValueError('Creation verification requires a qualified scope and explicit omissions.')
        if len(json.dumps(value,ensure_ascii=False,separators=(',',':'),allow_nan=False).encode())>1100:
            raise ValueError('Creation verification exceeds 1100 UTF-8 bytes.')
        return value


    @model_validator(mode='after')
    def bounded_receipt(self):
        if self.status == 'validated' and not self.receipt_id:
            raise ValueError('Validated stages require a host receipt identity.')
        if len(self.model_dump_json().encode('utf-8')) > MAX_RESULT_BYTES:
            raise ValueError('Receipt exceeds 16 KiB; store details behind a handle.')
        return self


StyleId = Annotated[str, Field(pattern=r'^[A-Za-z][A-Za-z0-9_-]{0,63}$')]


class WordStyle(Contract):
    id: StyleId
    font: str = Field(default='Arial',min_length=1,max_length=80)
    size_half_points: int | None = Field(default=None,ge=12,le=288)
    size_points: float | None = Field(default=None,ge=6,le=144,multiple_of=0.5)
    color: str = Field(default='202020',pattern=r'^[0-9A-Fa-f]{6}$')
    bold: bool = False
    italic: bool = False
    spacing_after_twips: int = Field(default=120,ge=0,le=2880)
    alignment: Literal['left','center','right','both'] = 'left'
    heading_level: int | None = Field(default=None,ge=1,le=6)


    @model_validator(mode='after')
    def unambiguous_size(self):
        if self.size_points is not None and self.size_half_points is not None:
            raise ValueError('Supply font size in points or half-points, not both.')
        return self


class PageSettings(Contract):
    width_twips: int = Field(default=11906,ge=2880,le=31680)
    height_twips: int = Field(default=16838,ge=2880,le=31680)
    margin_twips: int = Field(default=1134,ge=0,le=4320,
        description='Uniform margin on EACH side, in twips (1440 per inch); not the sum of opposite margins.')
    header_distance_twips: int = Field(default=567,ge=0,le=4320)
    footer_distance_twips: int = Field(default=567,ge=0,le=4320)

    @model_validator(mode='after')
    def usable_area(self):
        if 2*self.margin_twips+1440>min(self.width_twips,self.height_twips):
            raise ValueError('Page margins leave less than one inch of usable space.')
        return self


class LiteralTextReference(Contract):
    ref: str = Field(pattern=r'^ct_[0-9a-f]{24}$')


class ParagraphBlock(Contract):
    kind: Literal['paragraph'] = 'paragraph'
    text: Annotated[str,Field(max_length=8000)] | LiteralTextReference = Field(
        description='For drafting, perform the requested act: invite, request, explain, etc. Do not substitute a description of the document. Match requested language, tone and length.')
    style: StyleId = 'Normal'
    list_kind: Literal['none','bullet','number'] = 'none'
    page_break_before: bool = False


class TableBlock(Contract):
    kind: Literal['table'] = 'table'
    rows: list[list[Annotated[str, Field(max_length=2000)] | LiteralTextReference]] = Field(min_length=1,max_length=20)
    header_row: bool = True

    @model_validator(mode='after')
    def rectangular(self):
        columns=len(self.rows[0])
        if not 1<=columns<=12 or any(len(row)!=columns for row in self.rows):
            raise ValueError('Tables require 1–12 columns and rectangular rows.')
        return self


class PageBreakBlock(Contract):
    kind: Literal['page_break'] = 'page_break'


class TocBlock(Contract):
    kind: Literal['table_of_contents'] = 'table_of_contents'
    max_level: int = Field(default=3,ge=1,le=6)


Block = Annotated[ParagraphBlock | TableBlock | PageBreakBlock | TocBlock, Field(discriminator='kind')]


class PageBand(Contract):
    text: Annotated[str,Field(max_length=2000)] | LiteralTextReference = ''
    style: StyleId = 'Normal'
    alignment: Literal['left','center','right'] = 'left'
    page_numbers: Literal['none','current','current_total'] = Field(default='none',description='Add fields only when requested for THIS band. Text-only headers/footers omit this or use none.')
    separator: str = Field(default=' / ',max_length=40)

    @model_validator(mode='after')
    def valid_text(self):
        for text in (self.text,self.separator):
            if isinstance(text,LiteralTextReference):continue
            if any((ord(c)<32 and c not in '\n\t\r') or 0xD800<=ord(c)<=0xDFFF for c in text):
                raise ValueError('Header/footer contains invalid XML text.')
        return self


class CreateDocumentShape(Target):
    literal_refs: dict[Annotated[str,Field(max_length=100)],Annotated[str,Field(pattern=r'^ct_[0-9a-f]{24}$')]] = Field(default_factory=dict,max_length=24)
    tool: Literal['word.create'] = 'word.create'
    revision: Literal[None] = None
    styles: list[WordStyle] = Field(default_factory=list,max_length=16)
    page: PageSettings = Field(default_factory=PageSettings)
    blocks: list[Block] = Field(min_length=1,max_length=24)
    header: PageBand | None = None
    footer: PageBand | None = None
    first_header: PageBand | None = None
    first_footer: PageBand | None = None
    even_header: PageBand | None = None
    even_footer: PageBand | None = None


    @model_validator(mode='after')
    def safe_creation_payload(self):
        # These guards apply even to retained, not-yet-executable proposals.
        def check(value):
            if isinstance(value,str):
                if any((ord(c)<32 and c not in '\n\t\r') or
                       0xD800<=ord(c)<=0xDFFF or ord(c) in (0xFFFE,0xFFFF) for c in value):
                    raise ValueError('Document text contains invalid XML characters.')
            elif isinstance(value,dict):
                for item in value.values():check(item)
            elif isinstance(value,list):
                for item in value:check(item)
        check(self.model_dump())
        if len(self.model_dump_json().encode())>MAX_BATCH_BYTES:
            raise ValueError('Creation batch exceeds 32 KiB UTF-8.')
        return self



class CreateDocument(CreateDocumentShape):
    @model_validator(mode='after')
    def valid_design(self):
        names=[s.id for s in self.styles]
        if len(set(names))!=len(names):raise ValueError('Style IDs must be unique.')
        available=set(names)|{'Normal'}
        if sum(isinstance(block,TocBlock) for block in self.blocks)>1:
            raise ValueError('Creation supports one table of contents.')
        for block in self.blocks:
            if isinstance(block,TocBlock):
                headings={s.id for s in self.styles if s.heading_level and s.heading_level<=block.max_level}
                if not any(isinstance(p,ParagraphBlock) and p.style in headings for p in self.blocks):
                    raise ValueError('A table of contents requires a paragraph using a declared heading level in range.')
        if any(isinstance(block,ParagraphBlock) and block.style not in available for block in self.blocks):
            raise ValueError('Declare each paragraph style once before referring to it.')
        if any(band is not None and band.style not in available for band in (self.header,self.footer,self.first_header,self.first_footer,self.even_header,self.even_footer)):
            raise ValueError('Declare each header/footer style before referring to it.')
        for index,block in enumerate(self.blocks):
            if isinstance(block,PageBreakBlock):
                if index==0 or index==len(self.blocks)-1 or isinstance(self.blocks[index-1],PageBreakBlock):
                    raise ValueError('A page-break block must be between content blocks, without adjacent breaks.')
                continue
        return self


class CreationSourceBinding(Contract):
    slot: str = Field(min_length=1,max_length=100)
    ref: str = Field(pattern=r'^ct_[0-9a-f]{24}$')


class RepairCreation(Contract):
    tool: Literal['word.repair_creation'] = 'word.repair_creation'
    ref: str = Field(pattern=r'^cr_[0-9a-f]{32}$')
    styles: list[WordStyle] = Field(min_length=1,max_length=16)
    source_bindings: dict[Annotated[str,Field(max_length=100)],Annotated[str,Field(pattern=r'^ct_[0-9a-f]{24}$')]] = Field(default_factory=dict,max_length=24)
    bindings: list[CreationSourceBinding] = Field(default_factory=list,max_length=24)

    @model_validator(mode='after')
    def unambiguous_bindings(self):
        if self.bindings and self.source_bindings:
            raise ValueError('Use bindings or legacy source_bindings, not both nonempty forms.')
        slots=[item.slot for item in self.bindings]
        if len(slots)!=len(set(slots)):
            raise ValueError('Binding slots must be unique, even when references match.')
        return self



class PrepareCreation(CreateDocumentShape):
    tool: Literal['word.prepare_creation'] = 'word.prepare_creation'


class FinalizeCreation(RepairCreation):
    tool: Literal['word.finalize_creation'] = 'word.finalize_creation'
    styles: list[WordStyle] = Field(default_factory=list,max_length=16)


class CreationChange(Contract):
    op: Literal['set','remove','insert']
    path: str = Field(min_length=1,max_length=100)
    value: Any = None

    @model_validator(mode='after')
    def explicit_json_value(self):
        import math
        def valid(value,depth=0):
            if depth>32:return False
            if value is None or type(value) in (str,bool,int):return True
            if type(value) is float:return math.isfinite(value)
            if type(value) is list:return all(valid(x,depth+1) for x in value)
            if type(value) is dict:return all(type(k) is str and valid(v,depth+1) for k,v in value.items())
            return False
        if self.op!='remove' and 'value' not in self.model_fields_set:
            raise ValueError('Set/insert changes require an explicit value, including null.')
        if self.op=='remove' and self.value is not None:raise ValueError('Remove has no value.')
        if not valid(self.value):raise ValueError('Changes accept finite JSON values only, bounded to 32 nesting levels.')
        return self


class ReviseCreation(Contract):
    tool: Literal['word.revise_creation'] = 'word.revise_creation'
    ref: str = Field(pattern=r'^cr_[0-9a-f]{32}$')
    changes: list[CreationChange] = Field(min_length=1,max_length=24)

    @model_validator(mode='after')
    def bounded(self):
        if len(self.model_dump_json().encode())>MAX_BATCH_BYTES:raise ValueError('Proposal changes exceed 32 KiB.')
        return self


class RemoveCreationBlock(Contract):
    op: Literal['remove_block']
    index: int = Field(strict=True,ge=0,le=23)


class SetCreationParagraph(Contract):
    op: Literal['set_paragraph']
    index: int = Field(strict=True,ge=0,le=23)
    text: Annotated[str,Field(max_length=8000)] | LiteralTextReference | None = None
    style: StyleId | None = None
    page_break_before: Annotated[bool,Field(strict=True)] | None = None

    @model_serializer(mode='wrap')
    def sparse_serialization(self,handler):
        return {key:value for key,value in handler(self).items() if value is not None}

    @model_validator(mode='after')
    def sparse_properties(self):
        supplied=self.model_fields_set & {'text','style','page_break_before'}
        if not supplied or any(getattr(self,key) is None for key in supplied):
            raise ValueError('Set at least one nonnull paragraph property; omit unchanged properties.')
        if isinstance(self.text,str) and len(self.text)>8000:
            raise ValueError('Paragraph text exceeds 8000 characters.')
        return self


class EditCreationBlocks(Contract):
    tool: Literal['word.edit_creation_blocks'] = 'word.edit_creation_blocks'
    ref: str = Field(pattern=r'^cr_[0-9a-f]{32}$')
    changes: list[Annotated[RemoveCreationBlock | SetCreationParagraph,Field(discriminator='op')]] = Field(min_length=1,max_length=4)

    @model_validator(mode='after')
    def bounded(self):
        if len(self.model_dump_json().encode())>MAX_BATCH_BYTES:
            raise ValueError('Proposal changes exceed 32 KiB.')
        return self


class _CreationStyleTypography(Contract):
    id: StyleId
    font: Annotated[str,Field(min_length=1,max_length=80)] | None = None
    size_points: Annotated[float,Field(ge=6,le=144,multiple_of=0.5,allow_inf_nan=False)] | None = None
    bold: Annotated[bool,Field(strict=True)] | None = None
    italic: Annotated[bool,Field(strict=True)] | None = None
    color: Annotated[str,Field(pattern=r'^[0-9A-Fa-f]{6}$')] | None = None

    @model_serializer(mode='wrap')
    def sparse_serialization(self,handler):
        return {key:value for key,value in handler(self).items() if value is not None}

    @model_validator(mode='after')
    def sparse_properties(self):
        supplied=self.model_fields_set & {'font','size_points','bold','italic','color'}
        if not supplied or any(getattr(self,key) is None for key in supplied):
            raise ValueError('Supply at least one nonnull typography property; omit unchanged properties.')
        return self


class DeclareCreationStyle(_CreationStyleTypography):
    op: Literal['declare']
    font: str = Field(min_length=1,max_length=80)
    size_points: float = Field(ge=6,le=144,multiple_of=0.5,allow_inf_nan=False)


class UpdateCreationStyle(_CreationStyleTypography):
    op: Literal['update']


class EditCreationStyles(Contract):
    tool: Literal['word.edit_creation_styles'] = 'word.edit_creation_styles'
    ref: str = Field(pattern=r'^cr_[0-9a-f]{32}$')
    changes: list[Annotated[DeclareCreationStyle | UpdateCreationStyle,Field(discriminator='op')]] = Field(min_length=1,max_length=4)

    @model_validator(mode='after')
    def bounded(self):
        if len({change.id for change in self.changes})!=len(self.changes):
            raise ValueError('Change each style ID at most once per batch.')
        if len(self.model_dump_json().encode())>MAX_BATCH_BYTES:
            raise ValueError('Proposal changes exceed 32 KiB.')
        return self
