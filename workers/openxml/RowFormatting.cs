using System.Text.Json;
using DocumentFormat.OpenXml;
using W = DocumentFormat.OpenXml.Wordprocessing;

static class RowFormatting
{
    static bool On(W.OnOffOnlyType? value) => value!=null && (value.Val==null || value.Val.Value!=W.OnOffOnlyValues.Off);
    static bool Header(W.TableRow row) => On(row.TableRowProperties?.GetFirstChild<W.TableHeader>());

    internal static object Describe(W.TableRow row)
    {
        var p=row.TableRowProperties;var height=p?.GetFirstChild<W.TableRowHeight>();
        return new { supported=true,interpretation="Direct row properties; inherited styles may affect appearance. Repeating headers must form the first contiguous rows. Exact height may clip text; oversized rows can exceed page space.",
            structure=new { row_index=row.Parent is W.Table table?(int?)table.Elements<W.TableRow>().ToList().IndexOf(row):null,physical_cell_count=row.Elements<W.TableCell>().Count() },
            properties=new { repeat_header=p?.GetFirstChild<W.TableHeader>() is {} h?(bool?)On(h):null,
                allow_split=p?.GetFirstChild<W.CantSplit>() is {} split?(bool?)!On(split):null,
                height=height==null?null:new { twips=height.Val?.Value,rule=height.HeightType?.InnerText=="atLeast"?"at_least":height.HeightType?.InnerText ?? "auto" } } };
    }

    internal static void Apply(W.TableRow row,JsonElement values)
    {
        if(row.Parent is not W.Table table)throw new InvalidDataException("Row requires a direct table parent");
        var p=row.TableRowProperties;
        if(p?.GetFirstChild<W.TableRowPropertiesChange>()!=null)throw new InvalidDataException("Tracked row properties require explicit support");
        if(p==null){p=new W.TableRowProperties();row.TableRowProperties=p;}
        foreach(var field in values.EnumerateObject())
        {
            var value=field.Value;if(value.ValueKind==JsonValueKind.Null)continue;
            switch(field.Name)
            {
                case "height":
                    int n=value.GetProperty("twips").GetInt32();if(n<20 || n>31680)throw new InvalidDataException("Row height outside bounds");
                    var rule=value.GetProperty("rule").GetString() switch { "at_least"=>W.HeightRuleValues.AtLeast,"exact"=>W.HeightRuleValues.Exact,_=>throw new InvalidDataException("Invalid row height rule") };
                    var existingHeight=p.GetFirstChild<W.TableRowHeight>();
                    if(p.Elements<W.TableRowHeight>().Count()==1 && existingHeight?.Val?.Value==(uint)n && existingHeight.HeightType?.Value==rule)break;
                    p.RemoveAllChildren<W.TableRowHeight>();
                    p.AppendChild(new W.TableRowHeight { Val=(uint)n,HeightType=rule });break;
                case "repeat_header":
                    if(table.ChildElements.Any(e=>e is not W.TableProperties && e is not W.TableGrid && e is not W.TableRow))
                        throw new InvalidDataException("Wrapped table rows require explicit header support");
                    bool enabled=value.GetBoolean();var rows=table.Elements<W.TableRow>().ToList();int index=rows.IndexOf(row);
                    if(enabled && rows.Take(index).Any(r=>!Header(r)))throw new InvalidDataException("Repeating headers must start at the first row; enable rows top-down");
                    if(!enabled && rows.Skip(index+1).Any(Header))throw new InvalidDataException("Disable repeating header rows bottom-up to keep a contiguous prefix");
                    if(p.Elements<W.TableHeader>().Count()==1 && Header(row)==enabled)break;
                    p.RemoveAllChildren<W.TableHeader>();
                    p.AppendChild(new W.TableHeader { Val=enabled?W.OnOffOnlyValues.On:W.OnOffOnlyValues.Off });break;
                case "allow_split":
                    if(p.Elements<W.CantSplit>().Count()==1 && On(p.GetFirstChild<W.CantSplit>())==!value.GetBoolean())break;
                    p.RemoveAllChildren<W.CantSplit>();
                    p.AppendChild(new W.CantSplit { Val=value.GetBoolean()?W.OnOffOnlyValues.Off:W.OnOffOnlyValues.On });break;
                default:throw new InvalidDataException("Unknown row format property");
            }
        }
    }
}
