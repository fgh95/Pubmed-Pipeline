from pyspark.sql.functions import col
from pyspark.sql.functions import pandas_udf
from pyspark.sql.types import StringType, IntegerType
import pandas as pd
import os
import joblib
import time
import numpy as np
from glob import glob
import pubmed_parser as pp
from pyspark.sql import Row
from pyspark.sql import Window
from pyspark.sql.functions import rank, max, sum, desc, lit
from sklearn import *
import pickle
import datetime




class PubmedPipeline:

    def __init__(self, SPARK_CONTEXT, XMLFilesOutputPath, pipelinePath, numSlices, lastRunPicklePath):
        self.XMLFilesOutputPath = glob(XMLFilesOutputPath)
        self.pipelinePath = pipelinePath
        self.numSlices = numSlices
        self.lastRunPicklePath = lastRunPicklePath

        self.SPARK_CONTEXT = SPARK_CONTEXT

        self.pipeline = joblib.load(self.pipelinePath)

    
    def parseXMLToDF(self, xmlFiles, numSlices):
        medline_files_rdd = self.SPARK_CONTEXT.sparkContext.parallelize(xmlFiles, numSlices)

        parse_results_rdd = medline_files_rdd.\
        flatMap(lambda x: [Row(file_name=os.path.basename(x), **publication_dict) 
                        for publication_dict in pp.parse_medline_xml(x)])
        
        medline_df = parse_results_rdd.toDF()
        return medline_df
  

    ############## Cleaning function ########### 
 
  
    def cleanDataframe(self, dataframe):
        dataframe = dataframe.select("pmid", "pmc", "title", "medline_ta", "pubdate", "authors", "affiliations",
                                    "publication_types", "mesh_terms", "keywords", "chemical_list", "abstract", "country",
                                    "other_id", "doi", "nlm_unique_id", )

        dataframe = dataframe.withColumnRenamed("authors", "author").withColumnRenamed("affiliations", "affiliation")
        dataframe = dataframe.withColumn('pmid', dataframe['pmid'].cast(IntegerType()))
        return dataframe
  
  
    ############## Define UDF #########################

    def propagate_udf(self, *args):
      
        pipeline = self.pipeline
      
        @pandas_udf(returnType=StringType())
        def predict_pandas_udf(*features):
            X = pd.concat(features, axis=1)
            X.columns = ['abstract', 'title', 'medline_ta', 'keywords', 'publication_types', 'chemical_list', 'country',
                         'author', 'mesh_terms']
            y = pipeline.predict(X)
            return pd.Series(y)
        
        return predict_pandas_udf(*args)
    

    def applyClassifier(self, dataframe):
        dataframe = dataframe.withColumn( "prediction", self.propagate_udf(col("abstract"), col("title"), col("medline_ta"), col("keywords"), col("publication_types"),
                            col("chemical_list"), col("country"), col("author"), col("mesh_terms")))

        dataframe = dataframe.filter(dataframe.prediction == "Relevant")
        return dataframe
    

    def intersectPmidDataframes(self, currentDF, newRecords):
        return currentDF.select('pmid').intersect(newRecords.select('pmid'))


    def removeCommonPmidsFromDataframe(self, currentDF, commonPmids):
        left_join = currentDF.join(commonPmids, on=["pmid"], how='left_anti')
        left_join.show(n=1, truncate=False)
        return left_join


    def saveLastRunDate(self):
        today = datetime.date.today()
        pickle.dump(today, open(self.lastRunPicklePath, "wb"))
  
  



class PubmedPipelineSetup(PubmedPipeline):
    
    def __init__(self, SPARK, XMLFilesOutputPath, pipelinePath, mainDataframeOutputPath, numslices, lastRunPicklePath):
        super().__init__(SPARK, XMLFilesOutputPath, pipelinePath, numslices, lastRunPicklePath)
        self.mainDataframeOutputPath = mainDataframeOutputPath


    def runPipeline(self):
        dataframe = self.parseXMLToDF(self.XMLFilesOutputPath, self.numSlices)
        dataframe = self.cleanDataframe(dataframe)
        dataframe = self.applyClassifier(dataframe)

        dataframe.write.parquet(self.mainDataframeOutputPath, mode="overwrite")

        self.saveLastRunDate()





class PubmedPipelineUpdate(PubmedPipeline):

    def __init__(self, SPARK, XMLFilesOutputPath, pipelinePath, mainDataframePath, numslices, lastRunPicklePath, newAndUpdatedPapersDataframeOutputPath):
        super().__init__(SPARK, XMLFilesOutputPath, pipelinePath, numslices, lastRunPicklePath)
        self.mainDataframe = self.SPARK_CONTEXT.read.parquet(mainDataframePath)
        self.mainDataframePath = mainDataframePath
        self.newAndUpdatedPapersDataframeOutputPath = newAndUpdatedPapersDataframeOutputPath

    
    def runPipeline(self):
        # parse xml files into dataframe
        df = self.parseXMLToDF(self.XMLFilesOutputPath, self.numSlices)
        
        # df = df.repartition(8)
        # print("Total new papers to filter: " + str(df.count()))
        
        # clean
        df = self.cleanDataframe(df)

        # remove common papers from current dataframe
        commonPmids = self.intersectPmidDataframes(self.mainDataframe, df)
        print(commonPmids.count())
        self.mainDataframe = self.removeCommonPmidsFromDataframe(self.mainDataframe, commonPmids)
        print("After removing commons: " + str(self.mainDataframe.count()))

        # filter papers
        filteredDF = self.applyClassifier(df)
        print("new papers:" + str(filteredDF.count()))

        # adding new papers to main dataframe
        self.mainDataframe = self.mainDataframe.union(filteredDF)
        print("After union of new papers:" + str(self.mainDataframe.count()))
        
        # write final dataframe to parquet
        self.mainDataframe.write.parquet(self.mainDataframePath, mode='overwrite')
        
        # write updated dataframe to parquet
        filteredDF.write.parquet(self.newAndUpdatedPapersDataframeOutputPath, mode='overwrite')








