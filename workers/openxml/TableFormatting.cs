using System.Text.Json;
using DocumentFormat.OpenXml;
using W = DocumentFormat.OpenXml.Wordprocessing;

static class TableFormatting
{
    const string Namespace="http://schemas.openxmlformats.org/wordprocessingml/2006/main";
    static string? Attr(OpenXmlElement? e,string name) => e?.GetAttributes().FirstOrDefault(a=>a.LocalName==name && a.NamespaceUri==Namespace).Value;
    static void Set(OpenXmlElement e,string name,string value) => e.SetAttribute(new OpenXmlAttribute("w",name,Namespace,value));
    static IEnumerable<W.TableCell> Cells(W.Table t) => t.Elements<W.TableRow>().SelectMany(r=>r.Elements<W.TableCell>());

    internal static object Describe(W.Table t)
    {
        var p=t.GetFirstChild<W.TableProperties>();var columns=t.GetFirstChild<W.TableGrid>()?.Elements<W.GridColumn>().ToList() ?? new();
        return new { supported=true,interpretation="Direct table defaults; cell/row/style overrides may affect appearance. Width changes select fixed layout. Twips: 1440/inch.",
            properties=new { alignment=p?.TableJustification?.Val?.InnerText,width=Attr(p?.TableWidth,"w"),width_type=Attr(p?.TableWidth,"type"),layout=p?.TableLayout?.Type?.InnerText,
                column_widths_twips=columns.Take(12).Select(c=>c.Width?.Value),columns_truncated=columns.Count>12,total_columns=columns.Count,
                borders=p?.TableBorders?.ChildElements.Take(8).Select(b=>new { edge=b.LocalName,style=Attr(b,"val"),color=Attr(b,"color"),size_eighth_points=Attr(b,"sz") }),
                padding=p?.TableCellMarginDefault?.ChildElements.Take(6).Select(m=>new { edge=m.LocalName,width=Attr(m,"w"),type=Attr(m,"type") }) },
            structure=new { row_count=t.Elements<W.TableRow>().Count(),grid_column_count=columns.Count },
            overrides=new { cells_with_borders=Cells(t).Count(c=>c.TableCellProperties?.TableCellBorders!=null),
                cells_with_margins=Cells(t).Count(c=>c.TableCellProperties?.TableCellMargin!=null),
                row_exceptions=t.Elements<W.TableRow>().Count(r=>r.GetFirstChild<W.TablePropertyExceptions>()!=null) } };
    }

    internal static void Apply(W.Table table,JsonElement values)
    {
        var p=table.GetFirstChild<W.TableProperties>();
        if(p==null){p=new W.TableProperties();table.AddChild(p);}
        foreach(var field in values.EnumerateObject())
        {
            var value=field.Value;if(value.ValueKind==JsonValueKind.Null)continue;
            switch(field.Name)
            {
                case "column_widths":
                    decimal factor=value.GetProperty("unit").GetString() switch { "in"=>1440m,"cm"=>144000m/254m,"pt"=>20m,"twip"=>1m,_=>throw new InvalidDataException("Unknown width unit") };
                    Widths(table,p,value.GetProperty("values").EnumerateArray().Select(v=>checked((int)decimal.Round(v.GetDecimal()*factor,0,MidpointRounding.AwayFromZero))).ToList());break;
                case "column_widths_twips":Widths(table,p,value.EnumerateArray().Select(v=>v.GetInt32()).ToList());break;
                case "alignment":
                    if(p.TablePositionProperties!=null)throw new InvalidDataException("Floating table alignment requires a positioning operation");
                    p.TableJustification=new W.TableJustification { Val=value.GetString() switch { "left"=>W.TableRowAlignmentValues.Left,"center"=>W.TableRowAlignmentValues.Center,"right"=>W.TableRowAlignmentValues.Right,_=>throw new InvalidDataException("Invalid table alignment") } };break;
                case "borders":
                    string style=value.GetProperty("style").GetString()!;string color=value.GetProperty("color").GetString()!;int size=value.GetProperty("size_eighth_points").GetInt32();
                    if(!new[]{"single","double","dashed","dotted","nil"}.Contains(style) || !System.Text.RegularExpressions.Regex.IsMatch(color,"^[0-9A-Fa-f]{6}$") || size<2 || size>96)throw new InvalidDataException("Invalid table border");
                    p.TableBorders ??= new W.TableBorders();
                    // This deliberately changes table defaults, not direct cell overrides.
                    foreach(var border in new OpenXmlElement[]{new W.TopBorder(),new W.LeftBorder(),new W.BottomBorder(),new W.RightBorder(),new W.InsideHorizontalBorder(),new W.InsideVerticalBorder()}) {
                        Set(border,"val",style);Set(border,"color",color);Set(border,"sz",size.ToString());p.TableBorders.AddChild(border,true);
                    }
                    // Logical start/end otherwise override the explicitly replaced left/right defaults.
                    p.TableBorders.RemoveAllChildren<W.StartBorder>();p.TableBorders.RemoveAllChildren<W.EndBorder>();break;
                case "padding_top_twips":case "padding_bottom_twips":case "padding_left_twips":case "padding_right_twips":
                    int n=value.GetInt32();if(n<0 || n>2880)throw new InvalidDataException("Cell padding outside bounds");
                    p.TableCellMarginDefault ??= new W.TableCellMarginDefault();
                    OpenXmlElement margin=field.Name switch { "padding_top_twips"=>new W.TopMargin(),"padding_bottom_twips"=>new W.BottomMargin(),"padding_left_twips"=>new W.TableCellLeftMargin(),_=>new W.TableCellRightMargin() };
                    Set(margin,"w",n.ToString());Set(margin,"type","dxa");p.TableCellMarginDefault.AddChild(margin,true);
                    if(field.Name=="padding_left_twips")p.TableCellMarginDefault.RemoveAllChildren<W.StartMargin>();
                    if(field.Name=="padding_right_twips")p.TableCellMarginDefault.RemoveAllChildren<W.EndMargin>();break;
                default:throw new InvalidDataException("Unknown table format property");
            }
        }
    }

    static void Widths(W.Table table,W.TableProperties properties,List<int> widths)
    {
        if(widths.Count<1 || widths.Count>12 || widths.Any(w=>w<144 || w>31680) || widths.Sum()>31680)throw new InvalidDataException("Table widths outside bounds");
        var grid=table.GetFirstChild<W.TableGrid>() ?? throw new InvalidDataException("Table grid missing; cannot infer column positions");
        if(grid.Elements<W.GridColumn>().Count()!=widths.Count)throw new InvalidDataException("Provide one width per existing grid column");
        if(grid.ChildElements.Any(e=>e is not W.GridColumn))throw new InvalidDataException("Tracked table grid requires preservation-aware editing");
        if(table.ChildElements.Any(e=>e is not W.TableProperties && e is not W.TableGrid && e is not W.TableRow))throw new InvalidDataException("Wrapped table rows require explicit support");
        var changes=new List<(W.TableCell cell,int width)>();
        foreach(var row in table.Elements<W.TableRow>())
        {
            if(row.ChildElements.Any(e=>e is not W.TableRowProperties && e is not W.TableCell))throw new InvalidDataException("Wrapped cells or row exceptions require explicit width support");
            if(row.TableRowProperties?.GetFirstChild<W.GridBefore>()!=null || row.TableRowProperties?.GetFirstChild<W.GridAfter>()!=null)throw new InvalidDataException("Omitted edge cells require a grid-offset width operation");
            int offset=0;
            foreach(var cell in row.Elements<W.TableCell>())
            {
                if(cell.TableCellProperties?.GetFirstChild<W.HorizontalMerge>()!=null)throw new InvalidDataException("Legacy horizontal merges require normalization before width edits");
                int span=cell.TableCellProperties?.GridSpan?.Val?.Value ?? 1;
                if(span<1 || offset+span>widths.Count)throw new InvalidDataException("Cell span exceeds table grid");
                changes.Add((cell,widths.Skip(offset).Take(span).Sum()));offset+=span;
            }
            if(offset!=widths.Count)throw new InvalidDataException("Row cells do not cover the table grid");
        }
        int index=0;foreach(var column in grid.Elements<W.GridColumn>())column.Width=widths[index++].ToString();
        properties.TableWidth=new W.TableWidth { Type=W.TableWidthUnitValues.Dxa,Width=widths.Sum().ToString() };
        properties.TableLayout=new W.TableLayout { Type=W.TableLayoutValues.Fixed };
        foreach(var (cell,width) in changes) {
            cell.TableCellProperties ??= new W.TableCellProperties();
            cell.TableCellProperties.TableCellWidth=new W.TableCellWidth { Type=W.TableWidthUnitValues.Dxa,Width=width.ToString() };
        }
    }
}
