import java.nio.file.*;
import java.util.UUID;
import java.util.concurrent.TimeUnit;
import com.sun.star.beans.PropertyValue;
import com.sun.star.uno.UnoRuntime;
import com.sun.star.uno.XComponentContext;
import com.sun.star.bridge.XUnoUrlResolver;
import com.sun.star.comp.helper.Bootstrap;
import com.sun.star.frame.XComponentLoader;
import com.sun.star.frame.XStorable;
import com.sun.star.lang.XComponent;
import com.sun.star.text.XDocumentIndexesSupplier;
import com.sun.star.text.XDocumentIndex;
import com.sun.star.text.XTextFieldsSupplier;
import com.sun.star.util.XRefreshable;

/** Local deterministic pagination helper. Never edits the input file. */
public class UpdateFields {
    static PropertyValue property(String name,Object value) {
        PropertyValue p=new PropertyValue();p.Name=name;p.Value=value;return p;
    }
    public static void main(String[] args) throws Exception {
        if(args.length!=5)throw new IllegalArgumentException("soffice profile input output.pdf output.docx");
        String pipe="localoffice_"+UUID.randomUUID().toString().replace("-","");
        Process office=new ProcessBuilder(args[0],"-env:UserInstallation="+Path.of(args[1]).toUri(),
            "--headless","--nologo","--nodefault","--norestore","--nofirststartwizard",
            "--accept=pipe,name="+pipe+";urp;StarOffice.ComponentContext")
            .redirectOutput(ProcessBuilder.Redirect.DISCARD).redirectError(ProcessBuilder.Redirect.DISCARD).start();
        XComponent document=null;
        try {
            XComponentContext local=Bootstrap.createInitialComponentContext(null);
            XUnoUrlResolver resolver=UnoRuntime.queryInterface(XUnoUrlResolver.class,
                local.getServiceManager().createInstanceWithContext("com.sun.star.bridge.UnoUrlResolver",local));
            XComponentContext context=null;
            long deadline=System.nanoTime()+TimeUnit.SECONDS.toNanos(20);
            while(context==null && System.nanoTime()<deadline) {
                if(!office.isAlive())throw new IllegalStateException("LibreOffice exited before connection");
                try {context=UnoRuntime.queryInterface(XComponentContext.class,
                    resolver.resolve("uno:pipe,name="+pipe+";urp;StarOffice.ComponentContext"));}
                catch(com.sun.star.connection.NoConnectException e) {Thread.sleep(100);}
            }
            if(context==null)throw new IllegalStateException("LibreOffice connection timed out");
            XComponentLoader loader=UnoRuntime.queryInterface(XComponentLoader.class,
                context.getServiceManager().createInstanceWithContext("com.sun.star.frame.Desktop",context));
            document=loader.loadComponentFromURL(Path.of(args[2]).toUri().toString(),"_blank",0,new PropertyValue[]{
                property("Hidden",true),property("ReadOnly",false),property("MacroExecutionMode",(short)0),
                property("UpdateDocMode",(short)0),property("PickListEntry",false)});
            if(document==null)throw new IllegalStateException("Document did not load");
            var indexes=UnoRuntime.queryInterface(XDocumentIndexesSupplier.class,document).getDocumentIndexes();
            var fields=UnoRuntime.queryInterface(XTextFieldsSupplier.class,document).getTextFields();
            // Require stable index text/page references after re-pagination.
            String previous=null;boolean stable=false;int passes=0;
            for(int pass=0;pass<6;pass++) {
                UnoRuntime.queryInterface(XRefreshable.class,document).refresh();
                UnoRuntime.queryInterface(XRefreshable.class,fields).refresh();
                for(int i=0;i<indexes.getCount();i++)
                    UnoRuntime.queryInterface(XDocumentIndex.class,indexes.getByIndex(i)).update();
                StringBuilder signature=new StringBuilder();
                for(int i=0;i<indexes.getCount();i++)
                    signature.append(UnoRuntime.queryInterface(XDocumentIndex.class,indexes.getByIndex(i)).getAnchor().getString()).append('\u0000');
                var enumeration=fields.createEnumeration();
                while(enumeration.hasMoreElements())
                    signature.append(UnoRuntime.queryInterface(com.sun.star.text.XTextField.class,enumeration.nextElement()).getPresentation(false)).append('\u0000');
                passes++;
                if(signature.toString().equals(previous)){stable=true;break;}
                previous=signature.toString();
            }
            if(!stable)throw new IllegalStateException("Field pagination did not stabilize within six passes");
            XStorable store=UnoRuntime.queryInterface(XStorable.class,document);
            store.storeToURL(Path.of(args[3]).toUri().toString(),new PropertyValue[]{
                property("FilterName","writer_pdf_Export"),property("Overwrite",false)});
            store.storeToURL(Path.of(args[4]).toUri().toString(),new PropertyValue[]{
                property("FilterName","Office Open XML Text"),property("Overwrite",false)});
            System.out.println("{\"indexes_updated\":"+indexes.getCount()+",\"update_passes\":"+passes+"}");
        } finally {
            try {if(document!=null)document.dispose();}
            finally {
                office.destroy();
                if(!office.waitFor(3,TimeUnit.SECONDS))office.destroyForcibly();
            }
        }
    }
}
