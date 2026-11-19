using DocumentFormat.OpenXml;
using W = DocumentFormat.OpenXml.Wordprocessing;

static class TableSplitting
{
    static List<(W.TableCell cell,int start,int span)> Layout(W.TableRow row,int columns)
    {
        if(row.ChildElements.Any(e=>e is not W.TableRowProperties && e is not W.TableCell)
            || row.TableRowProperties?.GetFirstChild<W.GridBefore>()!=null || row.TableRowProperties?.GetFirstChild<W.GridAfter>()!=null)
            throw new InvalidDataException("Unmerge requires unwrapped rows without omitted edge cells");
        var result=new List<(W.TableCell,int,int)>();int offset=0;
        foreach(var cell in row.Elements<W.TableCell>())
        {
            int span=cell.TableCellProperties?.GridSpan?.Val?.Value ?? 1;
            if(span<1 || span>columns-offset)throw new InvalidDataException("Invalid cell span in merge grid");
            result.Add((cell,offset,span));offset+=span;
        }
        if(offset!=columns)throw new InvalidDataException("Row does not cover the table grid");
        return result;
    }
    static bool Continues(W.TableCell cell)
    {
        var merge=cell.TableCellProperties?.VerticalMerge;
        return merge!=null && (merge.Val==null || merge.Val.Value==W.MergedCellValues.Continue);
    }

    internal static bool Apply(W.TableCell anchor)
    {
        if(anchor.Parent is not W.TableRow row || row.Parent is not W.Table table)throw new InvalidDataException("Unmerge requires a direct table cell");
        if(table.ChildElements.Any(e=>e is not W.TableProperties && e is not W.TableGrid && e is not W.TableRow))throw new InvalidDataException("Wrapped table rows require explicit unmerge support");
        var grid=table.GetFirstChild<W.TableGrid>();
        if(grid==null || grid.ChildElements.Any(e=>e is not W.GridColumn))throw new InvalidDataException("Unmerge requires an untracked table grid");
        var columns=grid.Elements<W.GridColumn>().ToList();var rows=table.Elements<W.TableRow>().ToList();
        var start=Layout(row,columns.Count).Single(item=>ReferenceEquals(item.cell,anchor));
        if(start.span>12)throw new InvalidDataException("Unmerge supports at most 12 columns");
        if(Continues(anchor))throw new InvalidDataException("Select the top-left restart cell, not a vertical continuation");
        var affected=new List<W.TableCell> { anchor };
        if(anchor.TableCellProperties?.VerticalMerge!=null)
        {
            foreach(var next in rows.Skip(rows.IndexOf(row)+1))
            {
                var item=Layout(next,columns.Count).Single(c=>c.start<=start.start && c.start+c.span>start.start);
                if(!Continues(item.cell))break;
                if(item.start!=start.start || item.span!=start.span)throw new InvalidDataException("Mismatched vertical merge continuation");
                affected.Add(item.cell);
                if(affected.Count>20)throw new InvalidDataException("Unmerge supports at most 20 rows");
            }
        }
        foreach(var cell in affected)
        {
            if(cell.TableCellProperties?.GetFirstChild<W.HorizontalMerge>()!=null)throw new InvalidDataException("Legacy horizontal merge requires explicit support");
            if(cell.TableCellProperties?.Descendants().Any(e=>e.LocalName is "cellIns" or "cellDel" or "cellMerge" || e.LocalName.EndsWith("PrChange"))==true)throw new InvalidDataException("Tracked cell properties require explicit support");
        }
        if(start.span==1 && anchor.TableCellProperties?.VerticalMerge==null)return false;
        var widths=new List<int>();
        foreach(var column in columns.Skip(start.start).Take(start.span))
        {
            if(!int.TryParse(column.Width?.Value,out var width) || width<1 || width>31680)throw new InvalidDataException("Unmerge requires explicit positive grid widths");
            widths.Add(width);
        }
        foreach(var cell in affected)
        {
            cell.TableCellProperties ??= new W.TableCellProperties();var properties=cell.TableCellProperties;
            properties.RemoveAllChildren<W.GridSpan>();properties.RemoveAllChildren<W.VerticalMerge>();
            properties.TableCellWidth=new W.TableCellWidth { Type=W.TableWidthUnitValues.Dxa,Width=widths[0].ToString() };
            OpenXmlElement previous=cell;
            for(int c=1;c<start.span;c++)
            {
                var copy=(W.TableCellProperties)properties.CloneNode(true);
                copy.TableCellWidth=new W.TableCellWidth { Type=W.TableWidthUnitValues.Dxa,Width=widths[c].ToString() };
                var added=new W.TableCell(copy,new W.Paragraph());previous.InsertAfterSelf(added);previous=added;
            }
        }
        return true;
    }
}
