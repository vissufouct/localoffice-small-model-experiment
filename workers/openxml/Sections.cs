using System.Text.Json;
using W = DocumentFormat.OpenXml.Wordprocessing;

static class Sections
{
    internal static Dictionary<string,long?> Values(W.SectionProperties s) => new() {
        ["width_twips"]=s.GetFirstChild<W.PageSize>()?.Width?.Value,
        ["height_twips"]=s.GetFirstChild<W.PageSize>()?.Height?.Value,
        ["top_twips"]=s.GetFirstChild<W.PageMargin>()?.Top?.Value,
        ["bottom_twips"]=s.GetFirstChild<W.PageMargin>()?.Bottom?.Value,
        ["left_twips"]=s.GetFirstChild<W.PageMargin>()?.Left?.Value,
        ["right_twips"]=s.GetFirstChild<W.PageMargin>()?.Right?.Value,
        ["header_twips"]=s.GetFirstChild<W.PageMargin>()?.Header?.Value,
        ["footer_twips"]=s.GetFirstChild<W.PageMargin>()?.Footer?.Value,
        ["gutter_twips"]=s.GetFirstChild<W.PageMargin>()?.Gutter?.Value };

    internal static object Describe(W.SectionProperties s) => new {
        supported=true, interpretation="Explicit section geometry in twips (1440 per inch); missing values are unresolved",
        properties=Values(s).Where(kv=>kv.Value!=null).ToDictionary(kv=>kv.Key,kv=>kv.Value),
        orientation=s.GetFirstChild<W.PageSize>()?.Orient?.InnerText };

    internal static void Apply(W.SectionProperties s,JsonElement properties)
    {
        var values=Values(s);var changed=new HashSet<string>();
        foreach(var field in properties.EnumerateObject())
        {
            if(!values.ContainsKey(field.Name))throw new InvalidDataException("Unknown section layout property");
            if(field.Value.ValueKind==JsonValueKind.Null)continue;
            long value=field.Value.GetInt64();bool dimension=field.Name is "width_twips" or "height_twips";
            if(value<(dimension?1440:0) || value>(dimension?31680:14400))throw new InvalidDataException("Section layout value outside bounds");
            values[field.Name]=value;changed.Add(field.Name);
        }
        if(changed.Count==0 || changed.Contains("width_twips")!=changed.Contains("height_twips"))throw new InvalidDataException("Supply properties and both dimensions when resizing");
        if(values.Any(kv=>kv.Value==null))throw new InvalidDataException("Section geometry is unresolved; read and explicitly supply missing dimensions/margins");
        // Reserve the gutter on either axis: its placement can depend on document settings.
        long gutter=values["gutter_twips"]!.Value;
        if(values["width_twips"]-values["left_twips"]-values["right_twips"]-gutter<=0 ||
           values["height_twips"]-Math.Max(0,values["top_twips"]!.Value)-Math.Max(0,values["bottom_twips"]!.Value)-gutter<=0)
            throw new InvalidDataException("Section margins leave no usable text area");
        var size=s.GetFirstChild<W.PageSize>();var margin=s.GetFirstChild<W.PageMargin>();
        if(size==null){size=new W.PageSize();s.AddChild(size);}
        if(margin==null){margin=new W.PageMargin();s.AddChild(margin);}
        foreach(var key in changed)
        {
            int n=(int)values[key]!.Value;
            switch(key) {
                case "width_twips":size.Width=(uint)n;break;
                case "height_twips":size.Height=(uint)n;break;
                case "top_twips":margin.Top=n;break;
                case "bottom_twips":margin.Bottom=n;break;
                case "left_twips":margin.Left=(uint)n;break;
                case "right_twips":margin.Right=(uint)n;break;
                case "header_twips":margin.Header=(uint)n;break;
                case "footer_twips":margin.Footer=(uint)n;break;
                case "gutter_twips":margin.Gutter=(uint)n;break;
            }
        }
        if(changed.Contains("width_twips"))size.Orient=values["width_twips"]>values["height_twips"]?W.PageOrientationValues.Landscape:W.PageOrientationValues.Portrait;
    }
}
