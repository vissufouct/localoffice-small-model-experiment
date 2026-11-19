using DocumentFormat.OpenXml;
using W = DocumentFormat.OpenXml.Wordprocessing;

static class Tables
{
    internal static W.Table Create(List<List<string>> rows,List<int> widths,bool header,string style)
    {
        if(rows.Count is <1 or >20 || widths.Count is <1 or >12 || widths.Any(w=>w<144) || widths.Sum()>31680 || rows.Any(r=>r.Count!=widths.Count))
            throw new InvalidDataException("Invalid inserted table dimensions");
        var table=new W.Table(new W.TableProperties(
            new W.TableWidth { Type=W.TableWidthUnitValues.Dxa,Width=widths.Sum().ToString() },
            new W.TableBorders(new W.TopBorder { Val=W.BorderValues.Single,Size=4 },new W.LeftBorder { Val=W.BorderValues.Single,Size=4 },
                new W.BottomBorder { Val=W.BorderValues.Single,Size=4 },new W.RightBorder { Val=W.BorderValues.Single,Size=4 },
                new W.InsideHorizontalBorder { Val=W.BorderValues.Single,Size=4 },new W.InsideVerticalBorder { Val=W.BorderValues.Single,Size=4 })));
        var grid=new W.TableGrid();foreach(var width in widths)grid.Append(new W.GridColumn { Width=width.ToString() });table.Append(grid);
        for(int r=0;r<rows.Count;r++)
        {
            var row=new W.TableRow();if(r==0 && header)row.Append(new W.TableRowProperties(new W.TableHeader()));
            for(int c=0;c<widths.Count;c++)
            {
                var text=rows[r][c];if(text.EnumerateRunes().Count()>2000)throw new InvalidDataException("Inserted cell text limit exceeded");
                row.Append(new W.TableCell(new W.TableCellProperties(new W.TableCellWidth { Type=W.TableWidthUnitValues.Dxa,Width=widths[c].ToString() }),Creation.Paragraph(text,style)));
            }
            table.Append(row);
        }
        return table;
    }

    internal static string CellText(W.TableCell cell) => string.Join("\n",cell.Elements<W.Paragraph>().Select(p=>string.Concat(p.Descendants().Select(e=>e switch { W.Text t=>t.Text,W.Break=>"\n",W.TabChar=>"\t",_=>"" }))));

    static void PlainCell(W.TableCell cell)
    {
        if(!cell.ChildElements.All(e=>e is W.TableCellProperties || e is W.Paragraph))throw new InvalidDataException("Cell contains nested or unsupported content; use scoped paragraph tools");
        foreach(var p in cell.Elements<W.Paragraph>())
        {
            if(!p.ChildElements.All(e=>e is W.ParagraphProperties || e is W.Run))throw new InvalidDataException("Cell contains fields, links, bookmarks or controls; use scoped paragraph tools");
            if(p.Elements<W.Run>().Any(r=>!r.ChildElements.All(e=>e is W.RunProperties || e is W.Text || e is W.Break || e is W.TabChar)))throw new InvalidDataException("Cell contains non-text run content");
        }
    }

    internal static void SetText(W.TableCell cell,string text,bool force=false)
    {
        PlainCell(cell);
        text=text.Replace("\r\n","\n").Replace('\r','\n');
        if(!force && CellText(cell)==text)return;
        var old=cell.Elements<W.Paragraph>().ToList();
        var lines=text.Split('\n');
        if(lines.Length>24)throw new InvalidDataException("A cell edit supports at most 24 paragraphs per batch");
        var created=new List<W.Paragraph>();
        for(int i=0;i<lines.Length;i++)
        {
            var basis=old.Count>0?old[Math.Min(i,old.Count-1)]:null;
            var p=Creation.Paragraph(lines[i]);
            p.ParagraphProperties=basis?.ParagraphProperties is W.ParagraphProperties properties?(W.ParagraphProperties)properties.CloneNode(true):new W.ParagraphProperties();
            var originalRun=basis?.Elements<W.Run>().FirstOrDefault()?.RunProperties;
            if(originalRun!=null)p.GetFirstChild<W.Run>()!.RunProperties=(W.RunProperties)originalRun.CloneNode(true);
            created.Add(p);
        }
        foreach(var paragraph in old)paragraph.Remove();
        foreach(var paragraph in created)cell.AppendChild(paragraph);
    }

    internal static W.TableRow NewRow(W.TableRow anchor,List<string> values,bool header)
    {
        if(anchor.Parent is not W.Table table)throw new InvalidDataException("Row requires a direct table parent");
        if(table.Descendants<W.VerticalMerge>().Any())throw new InvalidDataException("Row insertion across vertical merges requires a qualified merge-aware operation");
        var cells=anchor.Elements<W.TableCell>().ToList();
        if(cells.Count!=values.Count)throw new InvalidDataException("Provide one value for each physical anchor cell, including horizontal spans");
        if(!anchor.ChildElements.All(e=>e is W.TableRowProperties || e is W.TableCell))throw new InvalidDataException("Row contains unsupported tracked or wrapped cells");
        var row=(W.TableRow)anchor.CloneNode(true);
        if(header) { row.TableRowProperties ??= new W.TableRowProperties();row.TableRowProperties.RemoveAllChildren<W.TableHeader>();row.TableRowProperties.AppendChild(new W.TableHeader()); }
        else row.TableRowProperties?.RemoveAllChildren<W.TableHeader>();
        int index=0;foreach(var cell in row.Elements<W.TableCell>())SetText(cell,values[index++],true);
        return row;
    }
}
