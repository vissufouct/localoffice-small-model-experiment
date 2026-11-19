using System.Text.Json;
using DocumentFormat.OpenXml;
using W = DocumentFormat.OpenXml.Wordprocessing;

static class CellFormatting
{
    const string Namespace="http://schemas.openxmlformats.org/wordprocessingml/2006/main";
    static string? Attr(OpenXmlElement? e,string name) => e?.GetAttributes().FirstOrDefault(a=>a.LocalName==name && a.NamespaceUri==Namespace).Value;
    static void Set(OpenXmlElement e,string name,string value) => e.SetAttribute(new OpenXmlAttribute("w",name,Namespace,value));

    internal static object Describe(W.TableCell cell)
    {
        var p=cell.TableCellProperties;var shading=p?.GetFirstChild<W.Shading>();
        return new { supported=true,interpretation="Direct physical-cell overrides. Omitted values inherit table/style defaults. A merged area may contain several physical cells; inspect and review all intended cells.",
            properties=new { fill=Attr(shading,"fill"),shading_pattern=Attr(shading,"val"),theme_fill=Attr(shading,"themeFill"),
                vertical_alignment=Attr(p?.GetFirstChild<W.TableCellVerticalAlignment>(),"val"),
                padding=p?.TableCellMargin?.ChildElements.Take(6).Select(m=>new { edge=m.LocalName,width=Attr(m,"w"),type=Attr(m,"type") }) },
            structure=new { row_index=cell.Parent is W.TableRow row && row.Parent is W.Table table?(int?)table.Elements<W.TableRow>().ToList().IndexOf(row):null,
                column_start=cell.Parent is W.TableRow parent?(long?)(parent.TableRowProperties?.GetFirstChild<W.GridBefore>()?.Val?.Value ?? 0)+parent.Elements<W.TableCell>().TakeWhile(c=>!ReferenceEquals(c,cell)).Sum(c=>(long)(c.TableCellProperties?.GridSpan?.Val?.Value ?? 1)):null,grid_span=p?.GridSpan?.Val?.Value ?? 1,vertical_merge=Attr(p?.GetFirstChild<W.VerticalMerge>(),"val"),
                has_vertical_merge=p?.GetFirstChild<W.VerticalMerge>()!=null,has_legacy_horizontal_merge=p?.GetFirstChild<W.HorizontalMerge>()!=null } };
    }

    internal static void Apply(W.TableCell cell,JsonElement values)
    {
        cell.TableCellProperties ??= new W.TableCellProperties();var p=cell.TableCellProperties;
        foreach(var field in values.EnumerateObject())
        {
            var value=field.Value;if(value.ValueKind==JsonValueKind.Null)continue;
            switch(field.Name)
            {
                case "fill":
                    string fill=value.GetString()!;
                    if(!System.Text.RegularExpressions.Regex.IsMatch(fill,"^(?:[0-9A-Fa-f]{6}|auto)$"))throw new InvalidDataException("Invalid cell fill");
                    // An explicit solid fill replaces theme/pattern attributes; omission preserves them.
                    p.AddChild(new W.Shading { Val=W.ShadingPatternValues.Clear,Color="auto",Fill=fill },true);break;
                case "vertical_alignment":
                    var alignment=value.GetString() switch { "top"=>W.TableVerticalAlignmentValues.Top,"center"=>W.TableVerticalAlignmentValues.Center,"bottom"=>W.TableVerticalAlignmentValues.Bottom,_=>throw new InvalidDataException("Invalid cell vertical alignment") };
                    p.AddChild(new W.TableCellVerticalAlignment { Val=alignment },true);break;
                case "padding_top_twips":case "padding_bottom_twips":case "padding_left_twips":case "padding_right_twips":
                    int n=value.GetInt32();if(n<0 || n>2880)throw new InvalidDataException("Cell padding outside bounds");
                    p.TableCellMargin ??= new W.TableCellMargin();
                    OpenXmlElement margin=field.Name switch { "padding_top_twips"=>new W.TopMargin(),"padding_bottom_twips"=>new W.BottomMargin(),"padding_left_twips"=>new W.LeftMargin(),_=>new W.RightMargin() };
                    Set(margin,"w",n.ToString());Set(margin,"type","dxa");p.TableCellMargin.AddChild(margin,true);
                    if(field.Name=="padding_left_twips")p.TableCellMargin.RemoveAllChildren<W.StartMargin>();
                    if(field.Name=="padding_right_twips")p.TableCellMargin.RemoveAllChildren<W.EndMargin>();break;
                default:throw new InvalidDataException("Unknown cell format property");
            }
        }
    }
}
