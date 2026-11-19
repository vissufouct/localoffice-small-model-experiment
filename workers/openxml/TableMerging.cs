using DocumentFormat.OpenXml;
using W = DocumentFormat.OpenXml.Wordprocessing;

static class TableMerging
{
    static bool Header(W.TableRow row)
    {
        var h=row.TableRowProperties?.GetFirstChild<W.TableHeader>();
        return h!=null && (h.Val==null || h.Val.Value!=W.OnOffOnlyValues.Off);
    }

    internal static void Apply(W.Table table,int rowStart,int rowCount,int columnStart,int columnCount)
    {
        if(rowStart<0 || rowCount is <1 or >20 || columnStart<0 || columnCount is <1 or >12 || rowCount*columnCount<2)
            throw new InvalidDataException("Invalid merge rectangle");
        if(table.ChildElements.Any(e=>e is not W.TableProperties && e is not W.TableGrid && e is not W.TableRow))
            throw new InvalidDataException("Wrapped or tracked table rows require explicit merge support");
        var rows=table.Elements<W.TableRow>().ToList();var grid=table.GetFirstChild<W.TableGrid>();
        if(grid==null || grid.ChildElements.Any(e=>e is not W.GridColumn))throw new InvalidDataException("Merge requires an untracked table grid");
        var columns=grid.Elements<W.GridColumn>().ToList();
        if(rowStart>rows.Count-rowCount || columnStart>columns.Count-columnCount)throw new InvalidDataException("Merge rectangle exceeds the table grid");
        int width=0;
        foreach(var column in columns.Skip(columnStart).Take(columnCount))
        {
            if(!int.TryParse(column.Width?.Value,out var n) || n<1 || n>31680)throw new InvalidDataException("Merge requires explicit positive grid widths");
            width+=n;
        }
        if(width>31680)throw new InvalidDataException("Merged width exceeds bounds");
        var selectedRows=rows.Skip(rowStart).Take(rowCount).ToList();
        if(selectedRows.Select(Header).Distinct().Count()>1)throw new InvalidDataException("A vertical merge cannot cross the repeating-header boundary");
        var region=new List<List<W.TableCell>>();
        foreach(var row in selectedRows)
        {
            if(row.ChildElements.Any(e=>e is not W.TableRowProperties && e is not W.TableCell)
                || row.TableRowProperties?.GetFirstChild<W.GridBefore>()!=null || row.TableRowProperties?.GetFirstChild<W.GridAfter>()!=null)
                throw new InvalidDataException("Wrapped or offset row cells require explicit merge support");
            var chosen=new List<W.TableCell>();int offset=0;
            foreach(var cell in row.Elements<W.TableCell>())
            {
                int span=cell.TableCellProperties?.GridSpan?.Val?.Value ?? 1;
                if(span<1 || span>columns.Count-offset)throw new InvalidDataException("Invalid cell grid span");
                if(offset<columnStart+columnCount && offset+span>columnStart)
                {
                    var p=cell.TableCellProperties;
                    if(span!=1 || p?.GetFirstChild<W.VerticalMerge>()!=null || p?.GetFirstChild<W.HorizontalMerge>()!=null)
                        throw new InvalidDataException("Merge region intersects an existing merge");
                    if(cell.ChildElements.Any(e=>e is not W.TableCellProperties && e is not W.Paragraph)
                        || cell.Descendants().Any(e=>e is W.FieldChar || e.LocalName is "ins" or "del" or "moveFrom" or "moveTo" or "cellIns" or "cellDel" or "cellMerge" or "bookmarkStart" or "bookmarkEnd" || e.LocalName.EndsWith("PrChange")))
                        throw new InvalidDataException("Merge region contains wrapped, nested, tracked, bookmarked or complex-field content requiring explicit support");
                    chosen.Add(cell);
                }
                offset+=span;
            }
            if(offset!=columns.Count || chosen.Count!=columnCount)throw new InvalidDataException("Rows do not cover the requested merge grid");
            region.Add(chosen);
        }
        var anchor=region[0][0];
        // Move existing paragraphs, not reconstructed text: runs and relationships remain intact.
        foreach(var cell in region.SelectMany(r=>r).Skip(1))
            foreach(var paragraph in cell.Elements<W.Paragraph>().ToList()) { paragraph.Remove();anchor.AppendChild(paragraph); }
        anchor.TableCellProperties ??= new W.TableCellProperties();
        anchor.TableCellProperties.GridSpan=new W.GridSpan { Val=columnCount };
        anchor.TableCellProperties.TableCellWidth=new W.TableCellWidth { Type=W.TableWidthUnitValues.Dxa,Width=width.ToString() };
        if(rowCount>1)anchor.TableCellProperties.VerticalMerge=new W.VerticalMerge { Val=W.MergedCellValues.Restart };
        for(int r=0;r<region.Count;r++)
        {
            var first=region[r][0];
            if(r>0)
            {
                first.TableCellProperties=(W.TableCellProperties)anchor.TableCellProperties.CloneNode(true);
                first.TableCellProperties.VerticalMerge=new W.VerticalMerge { Val=W.MergedCellValues.Continue };
                first.AppendChild(new W.Paragraph());
            }
            foreach(var cell in region[r].Skip(1))cell.Remove();
        }
    }
}
