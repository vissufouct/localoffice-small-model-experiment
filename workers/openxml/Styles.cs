using DocumentFormat.OpenXml;
using System.Text.Json;
using W = DocumentFormat.OpenXml.Wordprocessing;

static class Styles
{
    internal static (W.Style style,bool added) Create(W.Styles styles,W.Style basis,JsonElement op,bool character=false)
    {
        var kind=character?W.StyleValues.Character:W.StyleValues.Paragraph;
        var inherit=!character || !op.TryGetProperty("inherit_base",out var inheritFlag) || inheritFlag.GetBoolean();
        if(inherit && basis.Type?.Value!=kind)throw new InvalidDataException(character?"Base must be a character style":"Base must be a paragraph style");
        var id=op.GetProperty("style_id").GetString()!;var name=op.GetProperty("name").GetString()!;
        if(!System.Text.RegularExpressions.Regex.IsMatch(id,@"^[A-Za-z][A-Za-z0-9_-]{0,63}$") || name.Length>120 || string.IsNullOrWhiteSpace(name) || name.Any(c=>c<32))
            throw new InvalidDataException("Invalid new style identity");
        var seen=new HashSet<string>{id};W.Style? parent=inherit?basis:null;
        while(parent!=null)
        {
            var key=parent.StyleId?.Value;
            if(key==null || !seen.Add(key) || parent.Type?.Value!=kind)
                throw new InvalidDataException("Style inheritance cycle or incompatible base");
            var ancestor=parent.BasedOn?.Val?.Value;
            if(ancestor==null)break;
            parent=styles.Elements<W.Style>().SingleOrDefault(s=>s.StyleId?.Value==ancestor);
            if(parent==null)throw new InvalidDataException("Unresolved base style inheritance");
        }
        var created=new W.Style { Type=kind,StyleId=id,CustomStyle=true };
        created.Append(new W.StyleName { Val=name });
        if(inherit)created.Append(new W.BasedOn { Val=basis.StyleId!.Value });
        if(op.TryGetProperty("properties",out var properties) && properties.ValueKind==JsonValueKind.Object)Apply(created,properties,typographyOnly:character);
        var existing=styles.Elements<W.Style>().SingleOrDefault(s=>s.StyleId?.Value==id);
        if(existing!=null)
        {
            if(existing.OuterXml==created.OuterXml)return (existing,false);
            throw new InvalidDataException("Style ID already exists; creation cannot overwrite it");
        }
        if(styles.Elements<W.Style>().Any(s=>string.Equals(s.StyleName?.Val?.Value,name,StringComparison.OrdinalIgnoreCase)))
            throw new InvalidDataException("Style name already exists; choose a distinct name");
        styles.Append(created);return (created,true);
    }

    internal static void Rename(W.Styles styles,W.Style style,JsonElement op)
    {
        var name=op.GetProperty("name").GetString()!;
        if(string.IsNullOrWhiteSpace(name) || name.EnumerateRunes().Count()>120 || name.Any(c=>c<32))
            throw new InvalidDataException("Invalid style name");
        if(styles.Elements<W.Style>().Any(s=>!ReferenceEquals(s,style) && string.Equals(s.StyleName?.Val?.Value,name,StringComparison.OrdinalIgnoreCase)))
            throw new InvalidDataException("Style name already exists; choose a distinct name");
        if(style.StyleName is { } existing)existing.Val=name;
        else style.AddChild(new W.StyleName { Val=name });
    }

    internal static void Apply(W.Style style,JsonElement values,bool typographyOnly=false)
    {
        if(typographyOnly)
        {
            if(style.Type?.Value!=W.StyleValues.Paragraph && style.Type?.Value!=W.StyleValues.Character)
                throw new InvalidDataException("Style typography requires a paragraph or character style definition");
            if(!values.EnumerateObject().All(p=>new[]{"font","size_points","size_half_points","color","bold","italic"}.Contains(p.Name)))
                throw new InvalidDataException("Typography operation cannot change paragraph layout");
        }
        else if(style.Type?.Value!=W.StyleValues.Paragraph)
            throw new InvalidDataException("Style formatting currently requires a paragraph style definition");
        // Reuse the checked paragraph formatter without flattening inherited or
        // unrelated style settings. Only direct children are copied, never uses.
        var paragraph=new W.Paragraph();
        if(!typographyOnly && style.StyleParagraphProperties is { } p)
            paragraph.ParagraphProperties=new W.ParagraphProperties(p.ChildElements.Select(x=>x.CloneNode(true)));
        var run=new W.Run();
        if(style.StyleRunProperties is { } r)
            run.RunProperties=new W.RunProperties(r.ChildElements.Select(x=>x.CloneNode(true)));
        paragraph.Append(run);
        Formatting.Apply(paragraph,values);
        if(!typographyOnly && paragraph.ParagraphProperties is { } updatedP)
        {
            style.StyleParagraphProperties ??= new W.StyleParagraphProperties();
            style.StyleParagraphProperties.RemoveAllChildren();
            style.StyleParagraphProperties.Append(updatedP.ChildElements.Select(x=>x.CloneNode(true)));
        }
        if(run.RunProperties is { } updatedR)
        {
            style.StyleRunProperties ??= new W.StyleRunProperties();
            style.StyleRunProperties.RemoveAllChildren();
            style.StyleRunProperties.Append(updatedR.ChildElements.Select(x=>x.CloneNode(true)));
        }
    }

    // Direct definitions only. Bounded scalar properties avoid sending style XML
    // or implying that inheritance/theme/font resolution has been performed.
    internal static object Describe(W.Style style)
    {
        var values=new Dictionary<string,object?>();
        bool truncated=false;
        string? B(string? value) {
            if(value==null)return null;
            if(value.Length<=160)return value;
            truncated=true;return value[..160];
        }
        void Attr(string key,OpenXmlElement? node,string attribute="val") {
            if(node==null)return;
            var value=node.GetAttributes().FirstOrDefault(a=>a.LocalName==attribute && a.NamespaceUri=="http://schemas.openxmlformats.org/wordprocessingml/2006/main").Value;
            if(value!=null)values[key]=B(value);
        }
        values["style_id"]=B(style.StyleId?.Value);
        values["type"]=style.Type?.InnerText;
        values["default"]=style.Default?.Value ?? false;
        Attr("name",style.StyleName);Attr("based_on",style.BasedOn);
        Attr("next_style",style.NextParagraphStyle);Attr("linked_style",style.LinkedStyle);
        var p=style.StyleParagraphProperties;var r=style.StyleRunProperties;
        Attr("alignment",p?.GetFirstChild<W.Justification>());
        Attr("outline_level",p?.GetFirstChild<W.OutlineLevel>());
        Attr("spacing_before_twips",p?.GetFirstChild<W.SpacingBetweenLines>(),"before");
        Attr("spacing_after_twips",p?.GetFirstChild<W.SpacingBetweenLines>(),"after");
        Attr("left_indent_twips",p?.GetFirstChild<W.Indentation>(),"left");
        Attr("right_indent_twips",p?.GetFirstChild<W.Indentation>(),"right");
        Attr("first_line_twips",p?.GetFirstChild<W.Indentation>(),"firstLine");
        Attr("hanging_twips",p?.GetFirstChild<W.Indentation>(),"hanging");
        if(p?.GetFirstChild<W.KeepNext>() is W.KeepNext next)values["keep_with_next"]=next.Val?.Value ?? true;
        if(p?.GetFirstChild<W.KeepLines>() is W.KeepLines lines)values["keep_lines"]=lines.Val?.Value ?? true;
        if(p?.GetFirstChild<W.PageBreakBefore>() is W.PageBreakBefore page)values["page_break_before"]=page.Val?.Value ?? true;
        Attr("font",r?.GetFirstChild<W.RunFonts>(),"ascii");
        Attr("font_theme",r?.GetFirstChild<W.RunFonts>(),"asciiTheme");
        Attr("size_half_points",r?.GetFirstChild<W.FontSize>());
        Attr("color",r?.GetFirstChild<W.Color>());
        if(r?.GetFirstChild<W.Bold>() is W.Bold bold)values["bold"]=bold.Val?.Value ?? true;
        if(r?.GetFirstChild<W.Italic>() is W.Italic italic)values["italic"]=italic.Val?.Value ?? true;
        return new { supported=true,interpretation="Direct style properties only; inheritance and themes are not resolved. Other properties may exist.",properties=values,values_truncated=truncated };
    }
}
