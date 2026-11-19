using System.Text.Json;
using DocumentFormat.OpenXml;
using W = DocumentFormat.OpenXml.Wordprocessing;

static class Formatting
{
    internal static object Describe(OpenXmlElement element)
    {
        if(element is W.Drawing drawing)return ImageFormatting.Describe(drawing);
        if(element is W.Style style)return Styles.Describe(style);
        if(element is W.SectionProperties section)return Sections.Describe(section);
        if(element is W.Table table)return TableFormatting.Describe(table);
        if(element is W.TableRow row)return RowFormatting.Describe(row);
        if(element is W.TableCell cell)return CellFormatting.Describe(cell);
        if(element is W.NumberingInstance numbering)return Lists.Describe(numbering);
        if(element is not W.Paragraph paragraph)return new { supported=false };
        var p=paragraph.ParagraphProperties;
        var properties=new Dictionary<string,object?> {
            ["style_id"]=p?.ParagraphStyleId?.Val?.Value,["alignment"]=p?.Justification?.Val?.InnerText,
            ["number_id"]=p?.NumberingProperties?.NumberingId?.Val?.Value,["list_level"]=p?.NumberingProperties?.NumberingLevelReference?.Val?.Value,
            ["spacing_before_twips"]=p?.SpacingBetweenLines?.Before?.Value,["spacing_after_twips"]=p?.SpacingBetweenLines?.After?.Value,
            ["left_indent_twips"]=p?.Indentation?.Left?.Value,["right_indent_twips"]=p?.Indentation?.Right?.Value,
            ["first_line_twips"]=p?.Indentation?.FirstLine?.Value,["hanging_twips"]=p?.Indentation?.Hanging?.Value,
            ["keep_with_next"]=p?.KeepNext==null?null:(p.KeepNext.Val?.Value ?? true),
            ["keep_lines"]=p?.KeepLines==null?null:(p.KeepLines.Val?.Value ?? true),
            ["page_break_before"]=p?.PageBreakBefore==null?null:(p.PageBreakBefore.Val?.Value ?? true) };
        var runs=paragraph.Descendants<W.Run>().Where(r=>r.Ancestors<W.Paragraph>().FirstOrDefault()==paragraph).ToList();
        return new { supported=true,interpretation="Direct properties only; absent values may inherit styles or themes",properties=properties.Where(kv=>kv.Value!=null).ToDictionary(kv=>kv.Key,kv=>kv.Value),
            total_runs=runs.Count,runs_truncated=runs.Count>16,runs=runs.Take(16).Select((run,index)=>{
                var r=run.RunProperties;
                var values=new Dictionary<string,object?> { ["index"]=index,["style_id"]=r?.RunStyle?.Val?.Value,
                    ["font"]=r?.RunFonts?.Ascii?.Value,["size_half_points"]=r?.FontSize?.Val?.Value,["color"]=r?.Color?.Val?.Value,
                    ["bold"]=r?.Bold==null?null:(r.Bold.Val?.Value ?? true),["italic"]=r?.Italic==null?null:(r.Italic.Val?.Value ?? true) };
                return values.Where(kv=>kv.Value!=null).ToDictionary(kv=>kv.Key,kv=>kv.Value);
            }) };
    }

    internal static void Apply(W.Paragraph paragraph,JsonElement values)
    {
        bool points=values.TryGetProperty("size_points",out var pointSize) && pointSize.ValueKind!=JsonValueKind.Null;
        if(points)
        {
            if(values.TryGetProperty("size_half_points",out var half) && half.ValueKind!=JsonValueKind.Null)
                throw new InvalidDataException("Supply one font-size representation");
            var size=pointSize.GetDecimal();
            if(size<6 || size>144 || size*2!=decimal.Truncate(size*2))
                throw new InvalidDataException("Font size must be 6–144 points in half-point increments");
        }
        var properties=paragraph.ParagraphProperties ?? new W.ParagraphProperties();
        bool changedParagraph=false;
        foreach(var field in values.EnumerateObject())
        {
            var value=field.Value;
            if(value.ValueKind==JsonValueKind.Null)continue;
            switch(field.Name)
            {
                case "alignment":
                    properties.Justification=new W.Justification { Val=value.GetString() switch {
                        "left"=>W.JustificationValues.Left,"center"=>W.JustificationValues.Center,
                        "right"=>W.JustificationValues.Right,"both"=>W.JustificationValues.Both,
                        _=>throw new InvalidDataException("Unknown alignment") } };changedParagraph=true;break;
                case "spacing_before_twips":
                case "spacing_after_twips":
                    properties.SpacingBetweenLines ??= new W.SpacingBetweenLines();
                    if(field.Name=="spacing_before_twips") { properties.SpacingBetweenLines.Before=value.GetInt32().ToString();properties.SpacingBetweenLines.BeforeAutoSpacing=null;properties.SpacingBetweenLines.BeforeLines=null; }
                    else { properties.SpacingBetweenLines.After=value.GetInt32().ToString();properties.SpacingBetweenLines.AfterAutoSpacing=null;properties.SpacingBetweenLines.AfterLines=null; }
                    changedParagraph=true;break;
                case "left_indent_twips":
                case "right_indent_twips":
                case "first_line_twips":
                    properties.Indentation ??= new W.Indentation();
                    if(field.Name=="left_indent_twips") { properties.Indentation.Left=value.GetInt32().ToString();properties.Indentation.LeftChars=null; }
                    else if(field.Name=="right_indent_twips") { properties.Indentation.Right=value.GetInt32().ToString();properties.Indentation.RightChars=null; }
                    else {
                        int indent=value.GetInt32();properties.Indentation.FirstLineChars=null;properties.Indentation.HangingChars=null;
                        if(indent>=0) { properties.Indentation.FirstLine=indent.ToString();properties.Indentation.Hanging=null; }
                        else { properties.Indentation.FirstLine=null;properties.Indentation.Hanging=(-indent).ToString(); }
                    }
                    changedParagraph=true;break;
                case "keep_with_next":properties.KeepNext=new W.KeepNext { Val=value.GetBoolean() };changedParagraph=true;break;
                case "keep_lines":properties.KeepLines=new W.KeepLines { Val=value.GetBoolean() };changedParagraph=true;break;
                case "page_break_before":properties.PageBreakBefore=new W.PageBreakBefore { Val=value.GetBoolean() };changedParagraph=true;break;
                case "font":case "size_half_points":case "size_points":case "color":case "bold":case "italic":break;
                default:throw new InvalidDataException("Unknown paragraph format property");
            }
        }
        if(changedParagraph)paragraph.ParagraphProperties=properties;
        bool typography=values.EnumerateObject().Any(p=>p.Value.ValueKind!=JsonValueKind.Null && new[]{"font","size_half_points","size_points","color","bold","italic"}.Contains(p.Name));
        if(!typography)return;
        // Only runs belonging to this paragraph, not text inside nested drawings.
        var runs=paragraph.Descendants<W.Run>().Where(r=>r.Ancestors<W.Paragraph>().FirstOrDefault()==paragraph).ToList();
        if(runs.Count==0)runs.Add(paragraph.AppendChild(new W.Run()));
        foreach(var run in runs)
        {
            run.RunProperties ??= new W.RunProperties();var r=run.RunProperties;
            foreach(var field in values.EnumerateObject())
            {
                var value=field.Value;if(value.ValueKind==JsonValueKind.Null)continue;
                switch(field.Name)
                {
                    case "font":
                        r.RunFonts ??= new W.RunFonts();var font=value.GetString();
                        r.RunFonts.Ascii=font;r.RunFonts.HighAnsi=font;r.RunFonts.EastAsia=font;r.RunFonts.ComplexScript=font;
                        r.RunFonts.AsciiTheme=null;r.RunFonts.HighAnsiTheme=null;r.RunFonts.EastAsiaTheme=null;r.RunFonts.ComplexScriptTheme=null;break;
                    case "size_half_points":
                    case "size_points":
                        var size=(field.Name=="size_points"?decimal.ToInt32(value.GetDecimal()*2):value.GetInt32()).ToString(System.Globalization.CultureInfo.InvariantCulture);
                        r.FontSize=new W.FontSize { Val=size };r.FontSizeComplexScript=new W.FontSizeComplexScript { Val=size };break;
                    case "color":r.Color=new W.Color { Val=value.GetString() };break;
                    case "bold":r.Bold=new W.Bold { Val=value.GetBoolean() };r.BoldComplexScript=new W.BoldComplexScript { Val=value.GetBoolean() };break;
                    case "italic":r.Italic=new W.Italic { Val=value.GetBoolean() };r.ItalicComplexScript=new W.ItalicComplexScript { Val=value.GetBoolean() };break;
                }
            }
        }
    }
}
