using System.Text;
using System.Text.RegularExpressions;
using DocumentFormat.OpenXml;
using DocumentFormat.OpenXml.Packaging;
using W=DocumentFormat.OpenXml.Wordprocessing;

static class PageFields
{
    internal sealed record Field(string Code,List<W.Text> Texts,W.SimpleField? Simple,W.FieldChar? Begin,W.FieldChar? End)
    {
        internal string Value=>string.Concat(Texts.Select(t=>t.Text));
        internal bool Write(string value)
        {
            Check(value.Length is >0 and <=128,"Invalid rendered page-field result");
            Check(Simple?.FieldLock?.Value!=true && Begin?.FieldLock?.Value!=true,"Locked page field cannot be refreshed");
            Check(Texts.All(t=>t.Parent is W.Run),"Unsupported page-field cached content");
            var runs=Texts.Select(t=>(W.Run)t.Parent!).Distinct().ToList();
            Check(runs.Select(r=>r.RunProperties?.OuterXml??"").Distinct().Count()<=1,"Mixed formatting within numeric field cache is not qualified");
            bool changed=Value!=value || Simple?.Dirty?.Value==true || Begin?.Dirty?.Value==true;
            if(Value!=value)
            {
                if(Texts.Count==0)
                {
                    var run=new W.Run(new W.Text(value));
                    if(Simple!=null)Simple.Append(run);
                    else
                    {
                        Check(End?.Parent is W.Run,"Missing page-field end run");
                        if(Begin?.Parent is W.Run first && first.RunProperties!=null)run.PrependChild(first.RunProperties.CloneNode(true));
                        End!.Parent!.InsertBeforeSelf(run);
                    }
                }
                else {Texts[0].Text=value;foreach(var text in Texts.Skip(1))text.Text="";}
            }
            if(Simple?.Dirty?.Value==true)Simple.Dirty=false;
            if(Begin?.Dirty?.Value==true)Begin.Dirty=false;
            return changed;
        }
    }
    static void Check(bool ok,string message){if(!ok)throw new InvalidDataException(message);}
    internal static bool IsPage(string code)=>Regex.IsMatch(code,@"^\s*(PAGE|NUMPAGES)\b",RegexOptions.IgnoreCase);
    static string Code(string code)
    {
        var normalized=Regex.Replace(code.Trim(),@"\s+"," ");
        normalized=Regex.Replace(normalized,@"^\w+",m=>m.Value.ToUpperInvariant());
        return Regex.Replace(normalized,@"\s+\\\*\s+MERGEFORMAT\b","",RegexOptions.IgnoreCase);
    }
    internal static bool HasToc(OpenXmlElement root)=>root.Descendants<W.FieldCode>().Any(c=>Regex.IsMatch(c.Text,@"^\s*TOC\b",RegexOptions.IgnoreCase)) ||
        root.Descendants<W.SimpleField>().Any(c=>Regex.IsMatch(c.Instruction?.Value??"",@"^\s*TOC\b",RegexOptions.IgnoreCase));
    static bool InsideToc(W.Paragraph paragraph)=>paragraph.Ancestors().Any(a=>a.Parent is W.Body && HasToc(a)) ||
        (paragraph.Parent is W.Body && HasToc(paragraph));
    static List<W.Paragraph> Paragraphs(OpenXmlElement root)=>root.Descendants<W.Paragraph>().Where(p=>!InsideToc(p)).ToList();

    internal static (string text,List<Field> fields) Read(W.Paragraph paragraph)
    {
        var projection=new StringBuilder();var fields=new List<Field>();
        W.FieldChar? begin=null;bool separated=false;var code=new StringBuilder();var cached=new List<W.Text>();
        foreach(var node in paragraph.Descendants())
        {
            if(node.Ancestors<W.SimpleField>().Any())continue;
            if(node is W.SimpleField simple)
            {
                Check(begin==null,"Nested page-field container is not qualified");
                var instruction=simple.Instruction?.Value??"";
                if(IsPage(instruction))
                {
                    Check(simple.Descendants().All(e=>e is W.Run or W.RunProperties or W.Text || e.Ancestors<W.RunProperties>().Any()),"Unsupported simple page-field content");
                    var field=new Field(Code(instruction),simple.Descendants<W.Text>().ToList(),simple,null,null);
                    fields.Add(field);projection.Append("\u0001"+field.Code+"\u0002");
                }
                else projection.Append(string.Concat(simple.Descendants<W.Text>().Select(t=>t.Text)));
            }
            else if(node is W.FieldChar marker)
            {
                var kind=marker.FieldCharType?.Value;
                if(kind==W.FieldCharValues.Begin)
                {Check(begin==null,"Nested page fields are not qualified");begin=marker;separated=false;code.Clear();cached.Clear();}
                else if(kind==W.FieldCharValues.Separate)
                {
                    Check(begin!=null&&!separated,"Invalid field separator");separated=true;
                    if(IsPage(code.ToString()))projection.Append("\u0001"+Code(code.ToString())+"\u0002");
                }
                else if(kind==W.FieldCharValues.End)
                {
                    Check(begin!=null,"Invalid field end");
                    if(IsPage(code.ToString()))
                    {Check(separated,"Missing page-field separator");fields.Add(new Field(Code(code.ToString()),cached.ToList(),null,begin,marker));}
                    begin=null;
                }
            }
            else if(node is W.FieldCode instruction)
            {Check(begin!=null&&!separated,"Invalid field instruction");code.Append(instruction.Text);}
            else if(node is W.Text text)
            {
                if(begin!=null && separated && IsPage(code.ToString()))cached.Add(text);
                else projection.Append(text.Text);
            }
            else if(node is W.TabChar)projection.Append('\t');
            else if(node is W.Break br && br.Type?.Value!=W.BreakValues.Page)projection.Append('\n');
        }
        Check(begin==null,"Cross-paragraph page fields are not qualified");
        return (projection.ToString(),fields);
    }
    internal static string Projection(W.Paragraph paragraph)=>Read(paragraph).text;
    static bool HasPage(OpenXmlElement root)=>Paragraphs(root).Any(p=>Read(p).fields.Count>0);
    static int Transfer(OpenXmlElement source,OpenXmlElement rendered)
    {
        if(!HasPage(source))return 0;
        var original=Paragraphs(source);var updated=Paragraphs(rendered);
        Check(original.Count==updated.Count,"Renderer changed page-field paragraph topology");
        int count=0;
        for(int i=0;i<original.Count;i++)
        {
            var a=Read(original[i]);var b=Read(updated[i]);
            Check(a.text==b.text && a.fields.Count==b.fields.Count,"Renderer changed page-field paragraph content");
            for(int j=0;j<a.fields.Count;j++)
            {
                Check(a.fields[j].Code==b.fields[j].Code,"Renderer changed page-field options");
                if(a.fields[j].Write(b.fields[j].Value))count++;
            }
        }
        return count;
    }
    static Dictionary<string,string> References(W.Document document)
    {
        var result=new Dictionary<string,string>();int index=0;
        foreach(var section in document.Descendants<W.SectionProperties>())
        {
            foreach(var reference in section.ChildElements.OfType<W.HeaderFooterReferenceType>())
            {
                string key=index+"/"+reference.LocalName+"/"+reference.Type?.InnerText;
                Check(result.TryAdd(key,reference.Id!.Value!),"Duplicate section story reference");
            }
            index++;
        }
        return result;
    }
    internal static (Dictionary<string,OpenXmlPartRootElement> stories,int count) Apply(MainDocumentPart main,W.Document document,MainDocumentPart refreshed)
    {
        var before=Paragraphs(document);var after=Paragraphs(refreshed.Document!);
        Check(before.Count==after.Count,"Renderer changed body topology during page-field pagination");
        for(int i=0;i<before.Count;i++)
            Check(Projection(before[i])==Projection(after[i]),"Renderer changed body content during page-field pagination");
        int count=Transfer(document,refreshed.Document!);
        var stories=new Dictionary<string,OpenXmlPartRootElement>();
        var references=References(main.Document!);var other=References(refreshed.Document!);
        var expected=new Dictionary<string,string>();
        foreach(var (key,relationship) in references)
        {
            var part=main.GetPartById(relationship);
            OpenXmlPartRootElement? source=part switch { HeaderPart h=>h.Header,FooterPart f=>f.Footer,_=>null };
            if(source==null || !HasPage(source))continue;
            Check(other.TryGetValue(key,out var target),"Renderer changed section story identity");
            var generated=refreshed.GetPartById(target!);
            OpenXmlPartRootElement? rendered=generated switch { HeaderPart h=>h.Header,FooterPart f=>f.Footer,_=>null };
            Check(rendered!=null,"Missing rendered page-field story");
            var signature=string.Join("\u0000",Paragraphs(rendered!).Select(p=>{var r=Read(p);return r.text+"\u0001"+string.Join("\u0002",r.fields.Select(f=>f.Value));}));
            string name=part.Uri.ToString().TrimStart('/');
            if(expected.TryGetValue(name,out var prior))
            {Check(prior==signature,"Shared story has ambiguous cached page values");continue;}
            expected[name]=signature;
            var copy=(OpenXmlPartRootElement)source.CloneNode(true);
            int changed=Transfer(copy,rendered!);count+=changed;
            if(changed>0)stories[name]=copy;
        }
        return (stories,count);
    }
}
